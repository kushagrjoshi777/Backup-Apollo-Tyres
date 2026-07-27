import numpy as np


def parse_xsensor_csv(filepath):

    frames = {}

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    i = 0

    while i < len(lines):

        line = lines[i].strip()

        if line.startswith("FRAME"):

            try:
                frame_number = int(lines[i].split(",")[1])
            except:
                i += 1
                continue

            matrix_start = None

            for j in range(i, min(i + 30, len(lines))):

                if "SENSELS" in lines[j]:
                    matrix_start = j + 1
                    break

            if matrix_start is None:
                i += 1
                continue

            matrix = []

            for row_idx in range(matrix_start, matrix_start + 256):

                row = lines[row_idx].strip().split(",")

                values = []

                for val in row[:256]:

                    try:
                        values.append(float(val))
                    except:
                        values.append(0)

                matrix.append(values)

            frames[frame_number] = np.array(matrix)

            i = matrix_start + 256

        else:
            i += 1

    return frames