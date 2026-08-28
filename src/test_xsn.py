import numpy as np
import matplotlib.pyplot as plt

from xsn_parser import XSNReader, raw_to_psi


# ============================================================
# CHANGE THIS TO YOUR ACTUAL XSN FILE
# ============================================================

FILE = r"D:\Apollo\data\245_50R20_FL&RL_10kmph_raw data.xsn"


# ============================================================
# READ XSN
# ============================================================

with XSNReader(FILE) as reader:

    print("Frames:", len(reader))
    print("Frame rate:", reader.frame_rate())

    # Take the middle frame for testing
    frame = reader.get_frame(
        len(reader) // 2
    )


# ============================================================
# CONVERT RAW VALUES TO PSI (calibrated: raw / 128.0)
# ============================================================

psi = raw_to_psi(frame)

print(f"PSI range: {np.nanmin(psi):.2f} – {np.nanmax(psi):.2f}")
print(f"Non-zero sensels: {int(np.sum(psi > 0))}")


# ============================================================
# DISPLAY
# ============================================================

plt.figure(figsize=(8, 8))

plt.imshow(
    psi,
    cmap="jet",
    origin="lower",
    vmin=0,
    vmax=np.nanpercentile(psi, 99)
)

plt.colorbar(
    label="Pressure (PSI)"
)

plt.title("XSENSOR XSN Pressure Map (raw / 128)")

plt.xlabel("Sensor X")
plt.ylabel("Sensor Y")

plt.tight_layout()
plt.show()