import sys, os, glob, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from processor import load_and_process

files = sorted(glob.glob(r'd:\Apollo\data\*.*'))
print(f'Testing {len(files)} files...\n', flush=True)

for f in files:
    ext = os.path.splitext(f)[1].lower()
    if ext not in ('.csv', '.xsn'):
        continue
    print(f"-> Processing {os.path.basename(f)}...", flush=True)
    t0 = time.time()
    try:
        res = load_and_process(f, min_pressure=5.0, max_pressure=220.0, fft_keep_ratio=0.97)
        dt = time.time() - t0
        fn = res['frame_numbers']
        bm = res['best_metrics']
        cp = res['contact_patch']
        print(f"[{dt:.2f}s] OK: {os.path.basename(f)}", flush=True)
        print(f"       Frames: {len(fn)} | Best Frame: {res['best_frame_number']} | Active: {bm['active_sensels']} | Peak: {bm['peak_pressure_psi']} PSI | Load: {bm['estimated_load_kg']} kg | Bbox: {cp['width_mm']}x{cp['length_mm']} mm | Outline: {len(cp['outline_points'])} pts", flush=True)
    except Exception as e:
        dt = time.time() - t0
        print(f"[{dt:.2f}s] FAILED: {os.path.basename(f)} -> {e}", flush=True)

print('\nAll tests complete!', flush=True)


