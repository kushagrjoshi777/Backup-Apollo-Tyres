from src.parser import parse_xsensor_csv
from src.viewer import show_frame

CSV_FILE = r"data/295_90R20 EMA_120psi_static raw data with frame cut.csv"

print("Loading frames...")

frames = parse_xsensor_csv(CSV_FILE)

print(f"Frames Loaded: {len(frames)}")

print("Frame Numbers:")
print(list(frames.keys())[:10])

frame_number = 1822

show_frame(
    frames[frame_number],
    frame_number
)