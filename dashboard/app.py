"""
app.py  —  Apollo Tyre Dashboard  — Flask backend
"""

import os
import uuid
import json
import threading
import base64
import io
from datetime import datetime

from flask import (
    Flask, request, jsonify,
    send_from_directory, abort, send_file
)
from werkzeug.utils import secure_filename

from processor import load_and_process
from pdf_export import generate_report_pdf

# ── App setup ─────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR  = os.path.join(BASE_DIR, "static")
UPLOAD_DIR  = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024   # 2 GB

# ── In-memory job store ───────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}    # job_id → {"status", "result"|"error"}
_jobs_lock = threading.Lock()

ALLOWED_EXTS = {".csv"}


def _allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTS


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Receive CSV, kick off background processing, return job_id."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not _allowed(f.filename):
        return jsonify({"error": "Only .csv files are accepted"}), 400

    tyre_type = request.form.get("tyre_type", "TBR")
    try:
        min_pressure = float(request.form.get("min_pressure", 5.0))
        max_pressure = float(request.form.get("max_pressure", 220.0))
    except ValueError:
        min_pressure = 5.0
        max_pressure = 220.0

    try:
        speed = float(request.form.get("speed", 0.0))
    except ValueError:
        speed = 0.0

    filename  = secure_filename(f.filename)
    job_id    = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    f.save(save_path)

    with _jobs_lock:
        _jobs[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=_run_processing,
        args=(job_id, save_path, tyre_type, min_pressure, max_pressure, speed),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/status/<job_id>")
def status(job_id):
    """Poll job status.  Returns: {status, result?}  or  {status, error?}"""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        abort(404)

    return jsonify(job)


@app.route("/result/<job_id>")
def result(job_id):
    """Return full result for a completed job."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        abort(404)

    if job["status"] != "done":
        return jsonify({"error": "Not ready yet"}), 202

    return jsonify(job["result"])


@app.route("/export/<job_id>", methods=["POST"])
def export_pdf(job_id):
    """Generate and return PDF report for a job."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job["status"] != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    data = request.get_json()
    speed = data.get("speed", 0)
    canvas_data = data.get("canvas_data", "")  # Base64 PNG from frontend

    try:
        pdf_bytes = generate_report_pdf(job["result"], speed, canvas_data)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Apollo_TyrePressure_{int(speed)}kmph_Report.pdf"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Background worker ─────────────────────────────────────────────────────────

def _run_processing(job_id: str, filepath: str, tyre_type: str, min_pressure: float, max_pressure: float, speed: float):
    try:
        result = load_and_process(filepath, tyre_type=tyre_type, min_pressure=min_pressure, max_pressure=max_pressure, speed=speed)
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "result": result}
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "error": str(exc)}
    finally:
        # Clean up uploaded file to save disk space
        try:
            os.remove(filepath)
        except OSError:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("-" * 60)
    print("  Apollo Tyre Pressure Dashboard")
    print("  http://localhost:5050")
    print("-" * 60)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
