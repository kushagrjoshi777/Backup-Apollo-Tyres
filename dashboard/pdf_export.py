import io
import base64
import numpy as np
from PIL import Image as PILImage, ImageDraw
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak,
)

PINK = HexColor('#D81B60')
GRID = HexColor('#B0BEC5')

# Pre-compute Jet LUT (256 x 3)
JET_LUT = np.zeros((256, 3), dtype=np.uint8)
for _i in range(256):
    _t = _i / 255.0
    if _t < 0.125:
        _r, _g, _b = 0.0, 0.0, 0.5 + _t * 4.0
    elif _t < 0.375:
        _r, _g, _b = 0.0, (_t - 0.125) * 4.0, 1.0
    elif _t < 0.625:
        _r, _g, _b = (_t - 0.375) * 4.0, 1.0, 1.0 - (_t - 0.375) * 4.0
    elif _t < 0.875:
        _r, _g, _b = 1.0, 1.0 - (_t - 0.625) * 4.0, 0.0
    else:
        _r, _g, _b = 1.0 - (_t - 0.875) * 4.0, 0.0, 0.0
    JET_LUT[_i] = [
        int(np.clip(_r, 0, 1) * 255),
        int(np.clip(_g, 0, 1) * 255),
        int(np.clip(_b, 0, 1) * 255),
    ]

# (metric key, row label, decimals)
# Per stakeholder requirements:
# - Remove Maximum Length, Maximum Width, and Land/Sea Ratio
# - Add Perimeter and Gross Tyre Area (clearly distinguished from thresholded Contact Area)
# - 1 = Leading Edge; 2 = Trailing Edge; A = Outer Tyre Side; B = Inner Tyre Side
PARAMS = [
    ("contact_area_mm2",     "Contact area (mm\u00b2)",               1),
    ("contact_area_cm2",     "Contact area (cm\u00b2)",               2),
    ("gross_tyre_area_mm2",  "Gross tyre area (mm\u00b2)",            1),
    ("gross_tyre_area_cm2",  "Gross tyre area (cm\u00b2)",            2),
    ("perimeter_mm",         "Perimeter (mm)",                        1),
    ("contact_ratio_pct",    "Contact ratio (%)",                     1),
    ("fp_width_mm",          "FP width (mm)",                         1),
    ("fp_length_mm",         "FP length (mm)",                        1),
    ("outer_shoulder_s1_mm", "A \u2014 Outer tyre side S1 (mm)",      1),
    ("inner_shoulder_s2_mm", "B \u2014 Inner tyre side S2 (mm)",      1),
    ("shoulder_skewness_pct", "Shoulder skewness (%)",                1),
    ("squareness_s1_pct",    "Squareness A / S1 (%)",                 1),
    ("squareness_s2_pct",    "Squareness B / S2 (%)",                 1),
    ("squareness_avg_pct",   "Squareness avg (%)",                    1),
    ("contact_pressure_psi", "Contact pressure (PSI)",                1),
    ("peak_pressure_psi",    "Peak pressure (PSI)",                   1),
    ("estimated_load_kg",    "Estimated load (kg)",                   1),
]

FILTER_LABELS = {
    "none": "No filter",
    "gaussian": "Gaussian (spatial)",
    "median": "Median (spatial)",
    "bilateral": "Bilateral (edge-preserving)",
    "fft_butterworth_lp": "Fourier Butterworth low-pass",
    "fft_gaussian_lp": "Fourier Gaussian low-pass",
    "fft_ideal_lp": "Fourier ideal low-pass",
    "fft_notch": "Fourier spectral notch",
}


def _filter_description(cfg: dict) -> str:
    kind = cfg.get("filter_type", "none")
    label = FILTER_LABELS.get(kind, kind)
    extra = ""
    if kind == "fft_butterworth_lp":
        extra = f" (cutoff {cfg.get('fft_cutoff', 0):.2f} Nyq, order {cfg.get('fft_filter_strength', 2)})"
    elif kind in ("fft_gaussian_lp", "fft_ideal_lp"):
        extra = f" (cutoff {cfg.get('fft_cutoff', 0):.2f} Nyq)"
    elif kind == "fft_notch":
        extra = f" (sensitivity {cfg.get('notch_sensitivity', 0):.1f})"
    elif kind == "gaussian":
        extra = f" (sigma {cfg.get('gaussian_sigma', 0):.1f})"
    elif kind == "median":
        extra = f" ({cfg.get('median_size', 3)}x{cfg.get('median_size', 3)})"
    elif kind == "bilateral":
        extra = f" (colour {cfg.get('bilateral_sigma_color', 0):.0f} PSI, space {cfg.get('bilateral_sigma_space', 0):.1f})"
    return f"{label}{extra}; threshold {cfg.get('min_pressure', 18):.1f} PSI"


def _decode_frame(data: dict, fn: int) -> np.ndarray:
    rows, cols = int(data["grid_rows"]), int(data["grid_cols"])
    raw = data["frames"][str(fn)]
    if isinstance(raw, str):
        arr = np.frombuffer(base64.b64decode(raw), dtype=np.uint8)
    else:
        arr = np.asarray(raw, dtype=np.uint8)
    return arr.reshape(rows, cols)


def _render_tyre_png(u8: np.ndarray, outlines: list, pos: str = "FR", scale: int = 5) -> tuple[bytes, float]:
    """Jet heatmap + pink footprint boundary lines + 1/2 (Leading/Trailing) & A/B (Outer/Inner) callouts."""
    rgb = JET_LUT[u8].copy()
    rgb[u8 == 0] = [255, 255, 255]
    rows, cols = u8.shape
    hm_w, hm_h = cols * scale, rows * scale
    pad_top, pad_bot, pad_lr = 44, 44, 52
    canvas_w = hm_w + 2 * pad_lr
    canvas_h = hm_h + pad_top + pad_bot

    img = PILImage.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    hm_img = PILImage.fromarray(rgb).resize((hm_w, hm_h), PILImage.NEAREST)
    img.paste(hm_img, (pad_lr, pad_top))

    draw = ImageDraw.Draw(img)
    # Subtle frame around sensor grid
    draw.rectangle([pad_lr - 1, pad_top - 1, pad_lr + hm_w, pad_top + hm_h], outline=(220, 224, 230), width=2)

    # Pink footprint boundary lines (#FF1493 / #D81B60) with subtle white halo for contrast
    for flat in outlines or []:
        pts = [(pad_lr + flat[i] * scale, pad_top + flat[i + 1] * scale) for i in range(0, len(flat) - 1, 2)]
        if len(pts) >= 3:
            closed = pts + [pts[0]]
            draw.line(closed, fill=(255, 255, 255), width=6, joint="curve")
            draw.line(closed, fill=(233, 30, 99), width=4, joint="curve")

    # Edge & Side orientation labels:
    # 1 = Leading Edge (top); 2 = Trailing Edge (bottom)
    # A = Outer Tyre Side; B = Inner Tyre Side
    is_left_tyre = str(pos).upper() in ("FL", "RL")
    left_code = "A" if is_left_tyre else "B"
    right_code = "B" if is_left_tyre else "A"
    left_sub = "Outer" if is_left_tyre else "Inner"
    right_sub = "Inner" if is_left_tyre else "Outer"

    pink_rgb = (216, 27, 96)
    dark_rgb = (40, 48, 60)
    # Top: 1 (Leading Edge)
    draw.text((canvas_w // 2 - 42, 12), "1 (Leading Edge)", fill=pink_rgb)
    # Bottom: 2 (Trailing Edge)
    draw.text((canvas_w // 2 - 44, pad_top + hm_h + 14), "2 (Trailing Edge)", fill=pink_rgb)
    # Left & Right sides: A / B
    cy = pad_top + hm_h // 2
    draw.text((8, cy - 12), f"{left_code}", fill=pink_rgb)
    draw.text((4, cy + 2), f"({left_sub})", fill=dark_rgb)
    draw.text((pad_lr + hm_w + 14, cy - 12), f"{right_code}", fill=pink_rgb)
    draw.text((pad_lr + hm_w + 6, cy + 2), f"({right_sub})", fill=dark_rgb)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), canvas_h / canvas_w


def _placeholder_png(pos: str = "FL", w: int = 440, h: int = 420) -> bytes:
    img = PILImage.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([36, 28, w - 37, h - 29], outline=(215, 215, 215), width=2)
    is_left = str(pos).upper() in ("FL", "RL")
    l_lbl = "A (Outer)" if is_left else "B (Inner)"
    r_lbl = "B (Inner)" if is_left else "A (Outer)"
    d.text((w // 2 - 40, 8), "1 (Leading Edge)", fill=(170, 170, 170))
    d.text((w // 2 - 42, h - 22), "2 (Trailing Edge)", fill=(170, 170, 170))
    d.text((4, h // 2 - 6), l_lbl[:1], fill=(170, 170, 170))
    d.text((w - 20, h // 2 - 6), r_lbl[:1], fill=(170, 170, 170))
    d.text((w // 2 - 26, h // 2 - 6), "No data", fill=(170, 170, 170))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_colorbar_png(vmin: float, vmax: float) -> bytes:
    """Vertical Jet pressure scale bar PNG."""
    w, h = 60, 400
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        arr[y, :] = JET_LUT[int((1.0 - y / (h - 1)) * 255)]
    pil_img = PILImage.fromarray(arr)
    draw = ImageDraw.Draw(pil_img)
    ticks = [300, 284, 268, 253, 237, 222, 206, 191, 175, 160, 144, 129, 113, 98, 82, 67, 51, 36, 18]
    if vmax <= 220:
        ticks = [220, 205, 190, 175, 160, 145, 130, 115, 100, 85, 70, 55, 40, 25, int(round(vmin))]
    for val in ticks:
        norm = (val - vmin) / (vmax - vmin) if vmax > vmin else 0
        norm = float(np.clip(norm, 0, 1))
        y_pos = int((1.0 - norm) * (h - 20)) + 10
        draw.text((15, y_pos - 5), str(int(val)), fill=(255, 255, 255))
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()


def _fmt(val, dec: int) -> str:
    if val is None:
        return "\u2014"
    return f"{val:,.{dec}f}"


def _mean_metrics(rows: list) -> dict:
    out = {}
    for key, _label, _dec in PARAMS:
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        out[key] = float(np.mean(vals)) if vals else None
    return out


def _param_table(values: dict | None, width_mm: float, title: str | None = None) -> Table:
    """Compact two-column table (Parameter | Value) used directly under each tyre."""
    label_w = width_mm * 0.65
    rows = []
    if title:
        rows.append([title, ""])
    for key, label, dec in PARAMS:
        rows.append([label, _fmt(values.get(key), dec) if values else "\u2014"])
    t = Table(rows, colWidths=[label_w * mm, (width_mm - label_w) * mm])
    style = [
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 6.6),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, GRID),
        ('TOPPADDING', (0, 0), (-1, -1), 1.8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.8),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('FONTNAME', (1, 1 if not title else 1), (1, -1), 'Helvetica-Bold'),
    ]
    start = 1 if title else 0
    style.append(('ROWBACKGROUNDS', (0, start), (-1, -1), [white, HexColor('#F5F5F5')]))
    if title:
        style += [
            ('SPAN', (0, 0), (-1, 0)),
            ('BACKGROUND', (0, 0), (-1, 0), PINK),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ]
    t.setStyle(TableStyle(style))
    return t


def generate_report_pdf(data: dict, speed: float, frames: list) -> bytes:
    """
    Footprint Analysis Report (DYNAMIC MODEL).

    Computed ONLY from the explicitly selected frame(s) (1-3). Four tyre footprints sit
    side-by-side (FL | FR | RL | RR) with each tyre's parameter table directly beneath it.
    """
    buffer = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(
        buffer, pagesize=page,
        topMargin=6 * mm, bottomMargin=6 * mm, leftMargin=8 * mm, rightMargin=8 * mm,
        title="Footprint Analysis Report (Dynamic Model)",
    )
    styles = getSampleStyleSheet()
    speed_val = int(speed) if speed and speed > 0 else int(data.get("speed", 0) or 0)

    pos = (data.get("position") or "FR").upper()
    cfg = data.get("pipeline_config", {})
    metrics_by_fn = {m["frame_number"]: m for m in data.get("metrics", [])}
    sel = [fn for fn in frames if fn in metrics_by_fn]
    if not sel:
        raise ValueError("None of the selected frames exist in this analysis")
    mean_vals = _mean_metrics([metrics_by_fn[fn] for fn in sel])

    el = []
    title_style = ParagraphStyle(
        'MainTitle', parent=styles['Heading1'], fontSize=19, fontName='Helvetica-Bold',
        textColor=black, spaceAfter=1, alignment=0,
    )
    dyn_style = ParagraphStyle(
        'Dyn', parent=styles['Normal'], fontSize=10.5, fontName='Helvetica-Bold',
        textColor=PINK, spaceAfter=2,
    )
    sub_style = ParagraphStyle(
        'Sub', parent=styles['Normal'], fontSize=8, fontName='Helvetica',
        textColor=HexColor('#444444'), spaceAfter=1, leading=10.5,
    )
    el.append(Paragraph("Footprint Analysis Report", title_style))
    el.append(Paragraph("(DYNAMIC MODEL)", dyn_style))
    src = data.get("source_file") or ""
    pitch_w = data.get("sensel_w_mm", 1.56)
    pitch_h = data.get("sensel_h_mm", 1.56)
    el.append(Paragraph(
        f"Apollo Tyres &nbsp;|&nbsp; Speed {speed_val} km/h &nbsp;|&nbsp; Position {pos} "
        f"&nbsp;|&nbsp; {data.get('tyre_type', 'TBR')} (Threshold {data.get('vmin', 18.0):.0f} PSI, Pitch {pitch_w:.2f}&times;{pitch_h:.2f} mm) "
        f"&nbsp;|&nbsp; File {src}", sub_style))
    el.append(Paragraph(
        f"<b>Frames used ({len(sel)}):</b> {', '.join(str(f) for f in sel)} &nbsp;&mdash;&nbsp; "
        f"values are the {'mean of these frames' if len(sel) > 1 else 'values of this frame'}. "
        f"<b>Noise filter:</b> {_filter_description(cfg)}. "
        f"<b>Orientation Legend:</b> <b>1</b> = Leading Edge, <b>2</b> = Trailing Edge; "
        f"<b>A</b> = Outer Tyre Side, <b>B</b> = Inner Tyre Side.", sub_style))
    el.append(Spacer(1, 1.5 * mm))

    # ── Four tyres side by side, tables directly beneath ──
    COL_W = 62.0  # mm
    positions = ["FL", "FR", "RL", "RR"]
    primary = sel[0]
    u8 = _decode_frame(data, primary)
    outline = (data.get("outlines") or {}).get(str(primary), [])
    tyre_png, aspect = _render_tyre_png(u8, outline, pos=pos)

    img_w = 54.0
    img_h = img_w * aspect
    max_h = 52.0
    if img_h > max_h:
        img_h, img_w = max_h, max_h / aspect

    head_style = ParagraphStyle('Head', fontName='Helvetica-Bold', fontSize=11, alignment=1, textColor=white)
    headers, images, tables = [], [], []
    for p in positions:
        has = (p == pos)
        headers.append(Paragraph(p, head_style))
        if has:
            images.append(Image(io.BytesIO(tyre_png), width=img_w * mm, height=img_h * mm))
            tables.append(_param_table(mean_vals, COL_W - 3))
        else:
            ph_png = _placeholder_png(pos=p)
            images.append(Image(io.BytesIO(ph_png), width=img_w * mm, height=img_h * mm))
            tables.append(_param_table(None, COL_W - 3))

    cbar = Image(io.BytesIO(_generate_colorbar_png(data.get("vmin", 5), data.get("vmax", 220))),
                 width=14 * mm, height=min(max_h, 58) * mm)
    outer = Table(
        [headers + [''], images + [cbar], tables + ['']],
        colWidths=[COL_W * mm] * 4 + [18 * mm],
    )
    st = [
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 1.5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('VALIGN', (0, 1), (-1, 1), 'MIDDLE'),
    ]
    for ci, p in enumerate(positions):
        st.append(('BACKGROUND', (ci, 0), (ci, 0), PINK if p == pos else HexColor('#90A4AE')))
        st.append(('BOX', (ci, 0), (ci, 2), 0.8, GRID))
    outer.setStyle(TableStyle(st))
    el.append(outer)

    # ── Page 2: per-frame traceability when several frames were selected ──
    if len(sel) > 1:
        el.append(PageBreak())
        el.append(Paragraph("Selected frames \u2014 individual footprints", title_style))
        el.append(Paragraph(
            f"Position {pos} &nbsp;|&nbsp; the report values are the mean of these {len(sel)} frames.", sub_style))
        el.append(Spacer(1, 3 * mm))
        cells_h, cells_i, cells_t = [], [], []
        for fn in sel:
            png, asp = _render_tyre_png(_decode_frame(data, fn), (data.get("outlines") or {}).get(str(fn), []), pos=pos)
            w = 56.0
            h = w * asp
            if h > 58.0:
                h, w = 58.0, 58.0 / asp
            cells_h.append(Paragraph(f"Frame {fn}", head_style))
            cells_i.append(Image(io.BytesIO(png), width=w * mm, height=h * mm))
            cells_t.append(_param_table(metrics_by_fn[fn], 80))
        n = len(sel)
        t2 = Table([cells_h, cells_i, cells_t], colWidths=[88 * mm] * n)
        st2 = [
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('BACKGROUND', (0, 0), (-1, 0), PINK),
            ('BOX', (0, 0), (-1, -1), 0.4, GRID),
            ('INNERGRID', (0, 0), (-1, -1), 0.4, GRID),
            ('VALIGN', (0, 1), (-1, 1), 'MIDDLE'),
        ]
        t2.setStyle(TableStyle(st2))
        el.append(t2)

    doc.build(el)
    buffer.seek(0)
    return buffer.getvalue()
