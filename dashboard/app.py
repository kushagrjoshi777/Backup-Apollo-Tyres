"""
app.py  —  Apollo Tyre Dashboard  — Flask backend
Supports CSV and .XSN files.
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

ALLOWED_EXTS = {".csv", ".xsn"}


def _allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTS


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Receive CSV or XSN, kick off background processing, return job_id."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not _allowed(f.filename):
        return jsonify({"error": "Only .csv and .xsn files are accepted"}), 400

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

    try:
        fft_keep_ratio = float(request.form.get("fft_keep_ratio", 0.97))
        fft_keep_ratio = max(0.50, min(1.0, fft_keep_ratio))   # clamp [0.5, 1.0]
    except ValueError:
        fft_keep_ratio = 0.97

    position = request.form.get("position", "FR").upper()

    filename  = secure_filename(f.filename)
    job_id    = str(uuid.uuid4())
    save_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    f.save(save_path)

    with _jobs_lock:
        _jobs[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=_run_processing,
        args=(
            job_id, save_path, tyre_type,
            min_pressure, max_pressure,
            speed, position, filename, fft_keep_ratio
        ),
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

    data = request.get_json() or {}
    speed = data.get("speed", job["result"].get("speed", 0))
    canvas_data = data.get("canvas_data", "")  # Base64 PNG from frontend

    try:
        pdf_bytes = generate_report_pdf(job["result"], speed, canvas_data)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Apollo_TyreWear_Analysis_{int(speed)}kmph_Report.pdf"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Background worker ─────────────────────────────────────────────────────────

def _run_processing(
    job_id: str,
    filepath: str,
    tyre_type: str,
    min_pressure: float,
    max_pressure: float,
    speed: float,
    position: str,
    filename: str,
    fft_keep_ratio: float,
):
    try:
        # ── [XSN-DIAG] Upload traceability ────────────────────────────────
        ext = os.path.splitext(filename)[1].lower()
        fsize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        print(f"[XSN-DIAG] ═══════════════════════════════════════════════════")
        print(f"[XSN-DIAG] _run_processing() started")
        print(f"[XSN-DIAG]   job_id       : {job_id}")
        print(f"[XSN-DIAG]   filename     : {filename}")
        print(f"[XSN-DIAG]   extension    : {ext}")
        print(f"[XSN-DIAG]   saved path   : {filepath}")
        print(f"[XSN-DIAG]   file size    : {fsize:,} bytes ({fsize / 1048576:.1f} MB)")
        print(f"[XSN-DIAG]   calling      : processor.load_and_process()")
        print(f"[XSN-DIAG] ═══════════════════════════════════════════════════")
        # ─────────────────────────────────────────────────────────────────

        result = load_and_process(
            filepath,
            tyre_type=tyre_type,
            min_pressure=min_pressure,
            max_pressure=max_pressure,
            speed=speed,
            position=position,
            fft_keep_ratio=fft_keep_ratio,
        )
        result["source_file"] = filename

        # ── [XSN-DIAG] Result summary ─────────────────────────────────────
        print(f"[XSN-DIAG] Processing complete for {filename}")
        print(f"[XSN-DIAG]   playback frames : {len(result['frame_numbers'])}")
        print(f"[XSN-DIAG]   best frame      : {result['best_frame_number']}")
        print(f"[XSN-DIAG]   peak PSI        : {result['best_metrics']['peak_pressure_psi']}")
        print(f"[XSN-DIAG]   is_provisional  : {result.get('is_provisional')}")
        print(f"[XSN-DIAG]   PIPELINE: upload → app.py → processor.py → parser.py → xsn_parser.py → XSNReader → heatmap")
        print(f"[XSN-DIAG] ═══════════════════════════════════════════════════")
        # ─────────────────────────────────────────────────────────────────

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
