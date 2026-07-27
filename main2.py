from src.parser import parse_xsensor_csv

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

CSV_FILE = r"data\295_90R20 EMA_120psi_static raw data with frame cut.csv"

print("Loading frames...")

frames = parse_xsensor_csv(CSV_FILE)

frame_numbers = sorted(frames.keys())

print(f"Loaded {len(frame_numbers)} frames")

fig, ax = plt.subplots(figsize=(8, 8))

img = ax.imshow(
    frames[frame_numbers[0]],
    cmap="jet",
    origin="lower",
    vmin=0,
    vmax=150
)

cbar = plt.colorbar(img)
cbar.set_label("Pressure (PSI)")


def update(i):

    frame_no = frame_numbers[i]

    img.set_array(frames[frame_no])

    ax.set_title(f"Frame {frame_no}")

    return [img]


ani = FuncAnimation(
    fig,
    update,
    frames=len(frame_numbers),
    interval=20,
    blit=False,
    repeat=True
)

plt.show()