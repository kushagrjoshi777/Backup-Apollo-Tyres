"""
app.py  —  Apollo Tyre Dashboard  — Flask backend
Supports CSV and .XSN files.
"""

import os
import sys
import uuid
import json
import threading
import base64
import io
import csv
from datetime import datetime

# Ensure stdout/stderr handles UTF-8 safely on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


from flask import (
    Flask, request, jsonify,
    send_from_directory, abort, send_file
)
from werkzeug.utils import secure_filename

from processor import prepare_job, run_pipeline, config_with_overrides
from pdf_export import generate_report_pdf

# ── App setup ─────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
    APP_DIR  = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR  = BASE_DIR

# Include src directory in sys.path if present
src_path = os.path.join(BASE_DIR, "src")
if os.path.exists(src_path) and src_path not in sys.path:
    sys.path.insert(0, src_path)

STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024   # 2 GB

# ── In-memory job store ───────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}    # job_id → {"status", "result"|"error"}
_jobs_lock = threading.Lock()
_job_ctx: dict[str, dict] = {}       # job_id → cached pre-filter context (for live refilter)
_job_run_locks: dict[str, threading.Lock] = {}
_job_latest_seq: dict[str, int] = {}

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
        filters = json.loads(request.form.get("filters", "{}") or "{}")
        if not isinstance(filters, dict):
            filters = {}
    except ValueError:
        filters = {}

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
            speed, position, filename, filters
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


@app.route("/refilter/<job_id>", methods=["POST"])
def refilter(job_id):
    """Live noise-removal: re-run the filter pipeline on every frame with new settings."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        ctx = _job_ctx.get(job_id)
        run_lock = _job_run_locks.setdefault(job_id, threading.Lock())

    if job is None or job.get("status") != "done" or ctx is None:
        return jsonify({"error": "Job not found or not ready"}), 404

    data = request.get_json() or {}
    filters = data.get("filters") or {}
    seq = int(data.get("seq", 0))
    with _jobs_lock:
        _job_latest_seq[job_id] = max(seq, _job_latest_seq.get(job_id, 0))

    with run_lock:
        # A newer request already superseded this one while it waited – skip the work.
        if seq < _job_latest_seq.get(job_id, 0):
            return jsonify({"stale": True, "seq": seq})
        try:
            cfg = config_with_overrides(ctx["base_config"], filters)
            result = run_pipeline(ctx, cfg)
            result["source_file"] = job["result"].get("source_file", "")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "result": result}
    result["seq"] = seq
    return jsonify(result)


@app.route("/export/<job_id>", methods=["POST"])
def export_pdf(job_id):
    """Generate the Footprint Analysis Report from the explicitly selected frame(s) only."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job["status"] != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    data = request.get_json() or {}
    speed = data.get("speed", job["result"].get("speed", 0))
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 0.0

    try:
        selected = [int(f) for f in (data.get("frames") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid frame selection"}), 400
    selected = list(dict.fromkeys(selected))      # unique, order preserved
    if not selected:
        return jsonify({"error": "Select at least one frame for the report"}), 400
    if len(selected) > 3:
        return jsonify({"error": "A report can use at most 3 frames"}), 400
    valid = set(job["result"]["frame_numbers"])
    missing = [f for f in selected if f not in valid]
    if missing:
        return jsonify({"error": f"Frame(s) not available: {missing}"}), 400

    try:
        pdf_bytes = generate_report_pdf(job["result"], speed, selected)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Apollo_Footprint_Analysis_Report_{int(speed)}kmph.pdf"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _decode_pixel_data(pd: dict) -> dict:
    """Expand base64 typed buffers in pixel_data back into plain Python lists."""
    import numpy as np
    if pd.get("encoding") != "b64":
        return pd
    out = {k: v for k, v in pd.items() if k not in ("_dtypes", "encoding")}
    for name, dt in pd.get("_dtypes", {}).items():
        raw = pd.get(name) or ""
        out[name] = np.frombuffer(base64.b64decode(raw), dtype=np.dtype(dt)).tolist() if raw else []
    return out


@app.route("/pixel/<job_id>", methods=["POST"])
def pixel_inspector(job_id):
    """Pixel inspector endpoint. Returns values and flags for a specific pixel and its neighbourhood."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job.get("status") != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    data = request.get_json() or {}
    frame_number = data.get("frame_number")
    row = data.get("row")
    col = data.get("col")

    if frame_number is None or row is None or col is None:
        return jsonify({"error": "Missing frame_number, row, or col"}), 400

    result = job["result"]
    pixel_data = result.get("pixel_data")
    if not pixel_data:
        return jsonify({"error": "Pixel data not available"}), 404
    pixel_data = _decode_pixel_data(pixel_data)

    # The pixel_data stores flat arrays.
    cols = pixel_data.get("grid_cols", 256)
    idx = row * cols + col
    
    if idx < 0 or idx >= cols * pixel_data.get("grid_rows", 256):
        return jsonify({"error": "Row or col out of bounds"}), 400

    # Retrieve values safely
    raw_val = pixel_data.get("best_frame_raw", [])[idx] if len(pixel_data.get("best_frame_raw", [])) > idx else None
    cal_val = pixel_data.get("best_frame_calibrated", [])[idx] if len(pixel_data.get("best_frame_calibrated", [])) > idx else None
    filtered_val = pixel_data.get("best_frame_filtered", [])[idx] if len(pixel_data.get("best_frame_filtered", [])) > idx else None
    final_val = pixel_data.get("best_frame_final", [])[idx] if len(pixel_data.get("best_frame_final", [])) > idx else None

    invalid = pixel_data.get("invalid_mask", [])[idx] if len(pixel_data.get("invalid_mask", [])) > idx else False
    saturated = pixel_data.get("saturation_mask", [])[idx] if len(pixel_data.get("saturation_mask", [])) > idx else False
    spiked = pixel_data.get("spike_mask", [])[idx] if len(pixel_data.get("spike_mask", [])) > idx else False
    contact = pixel_data.get("contact_mask", [])[idx] if len(pixel_data.get("contact_mask", [])) > idx else False

    # Get 3x3 neighbourhood of final values
    neighbourhood = []
    rows = pixel_data.get("grid_rows", 256)
    final_arr = pixel_data.get("best_frame_final", [])
    if final_arr:
        for r in range(max(0, row - 1), min(rows, row + 2)):
            nrow = []
            for c in range(max(0, col - 1), min(cols, col + 2)):
                nidx = r * cols + c
                nrow.append(final_arr[nidx] if nidx < len(final_arr) else 0)
            neighbourhood.append(nrow)
            
    return jsonify({
        "raw_value": raw_val,
        "calibrated_psi": cal_val,
        "filtered_psi": filtered_val,
        "final_psi": final_val,
        "flags": {
            "invalid": bool(invalid),
            "saturated": bool(saturated),
            "spatial_spike": bool(spiked),
            "in_contact_patch": bool(contact)
        },
        "neighbourhood_3x3": neighbourhood
    })

@app.route("/diagnostics/<job_id>", methods=["GET"])
def get_diagnostics(job_id):
    """Return frame diagnostics array."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job.get("status") != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    return jsonify(job["result"].get("frame_diagnostics", []))

@app.route("/diagnostics/csv/<job_id>", methods=["GET"])
def get_diagnostics_csv(job_id):
    """Return frame diagnostics as CSV file download."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job.get("status") != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    diags = job["result"].get("frame_diagnostics", [])
    if not diags:
        return jsonify({"error": "No diagnostics available"}), 404
        
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=diags[0].keys())
    writer.writeheader()
    writer.writerows(diags)
    
    return send_file(
        io.BytesIO(si.getvalue().encode('utf-8')),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"diagnostics_{job_id}.csv"
    )

@app.route("/calibration/<job_id>", methods=["GET"])
def get_calibration(job_id):
    """Return calibration audit dict."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None or job.get("status") != "done":
        return jsonify({"error": "Job not found or not ready"}), 404

    return jsonify(job["result"].get("calibration_audit", {}))


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
    filters: dict,
):
    try:
        # ── [XSN-DIAG] Upload traceability ────────────────────────────────
        ext = os.path.splitext(filename)[1].lower()
        fsize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        print(f"[XSN-DIAG] ===========================================================")
        print(f"[XSN-DIAG] _run_processing() started")
        print(f"[XSN-DIAG]   job_id       : {job_id}")
        print(f"[XSN-DIAG]   filename     : {filename}")
        print(f"[XSN-DIAG]   extension    : {ext}")
        print(f"[XSN-DIAG]   saved path   : {filepath}")
        print(f"[XSN-DIAG]   file size    : {fsize:,} bytes ({fsize / 1048576:.1f} MB)")
        print(f"[XSN-DIAG]   calling      : processor.prepare_job() + run_pipeline()")
        print(f"[XSN-DIAG] ===========================================================")
        # -----------------------------------------------------------------

        ctx = prepare_job(
            filepath,
            tyre_type=tyre_type,
            min_pressure=min_pressure,
            max_pressure=max_pressure,
            speed=speed,
            position=position,
        )
        cfg = config_with_overrides(ctx["base_config"], filters)
        result = run_pipeline(ctx, cfg)
        result["source_file"] = filename
        with _jobs_lock:
            _job_ctx[job_id] = ctx

        # -- [XSN-DIAG] Result summary -------------------------------------
        print(f"[XSN-DIAG] Processing complete for {filename}")
        print(f"[XSN-DIAG]   playback frames : {len(result['frame_numbers'])}")
        print(f"[XSN-DIAG]   best frame      : {result['best_frame_number']}")
        print(f"[XSN-DIAG]   peak PSI        : {result['best_metrics']['peak_pressure_psi']}")
        print(f"[XSN-DIAG]   is_provisional  : {result.get('is_provisional')}")
        print(f"[XSN-DIAG]   PIPELINE: upload -> app.py -> processor.py -> parser.py -> xsn_parser.py -> XSNReader -> heatmap")
        print(f"[XSN-DIAG] ===========================================================")
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
    import webbrowser

    port = 5050
    url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print("  Apollo Tyre Pressure Analysis Application")
    print(f"  Launching web interface at: {url}")
    print("=" * 60)

    # Open browser after a brief delay
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
