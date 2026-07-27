import matplotlib.pyplot as plt


def show_frame(frame, frame_number):

    plt.figure(figsize=(8, 8))

    plt.imshow(
        frame,
        cmap="jet",
        origin="lower",
        vmin=0,
        vmax=150
    )

    plt.colorbar(label="Pressure (PSI)")

    plt.title(f"Frame {frame_number}")

    plt.tight_layout()

    plt.show()