import io
import base64
import numpy as np
from PIL import Image as PILImage, ImageDraw, ImageFont
from reportlab.lib.pagesizes import portrait, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white, lightgrey
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether

# Pre-compute Jet LUT (256 x 3)
JET_LUT = np.zeros((256, 3), dtype=np.uint8)
for i in range(256):
    t = i / 255.0
    if t < 0.125:
        r, g, b = 0.0, 0.0, 0.5 + t * 4.0
    elif t < 0.375:
        r, g, b = 0.0, (t - 0.125) * 4.0, 1.0
    elif t < 0.625:
        r, g, b = (t - 0.375) * 4.0, 1.0, 1.0 - (t - 0.375) * 4.0
    elif t < 0.875:
        r, g, b = 1.0, 1.0 - (t - 0.625) * 4.0, 0.0
    else:
        r, g, b = 1.0 - (t - 0.875) * 4.0, 0.0, 0.0
    JET_LUT[i] = [
        int(np.clip(r, 0, 1) * 255),
        int(np.clip(g, 0, 1) * 255),
        int(np.clip(b, 0, 1) * 255)
    ]


def _flat_array_to_png(flat_uint8: list) -> bytes:
    """Convert flat 256x256 uint8 array to Jet colormap PNG bytes."""
    arr = np.array(flat_uint8, dtype=np.uint8).reshape((256, 256))
    rgb = JET_LUT[arr]
    # For zero pressure sensels, set dark cyan background matching Img 1
    zero_mask = (arr == 0)
    rgb[zero_mask] = [220, 248, 252]  # Light cyan background as in Img 1
    pil_img = PILImage.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()


def _create_placeholder_png(label: str) -> bytes:
    """Create a clean light cyan placeholder PNG for non-uploaded slots."""
    pil_img = PILImage.new("RGB", (256, 256), color=(230, 250, 252))
    draw = ImageDraw.Draw(pil_img)
    # Draw simple frame
    draw.rectangle([10, 10, 245, 245], outline=(180, 220, 230), width=2)
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_colorbar_png(vmin: float, vmax: float) -> bytes:
    """Generate vertical Jet pressure scale bar PNG."""
    w, h = 60, 400
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        t = 1.0 - (y / (h - 1))  # 0 at bottom, 255 at top
        idx = int(t * 255)
        arr[y, :] = JET_LUT[idx]

    pil_img = PILImage.fromarray(arr, mode="RGB")
    draw = ImageDraw.Draw(pil_img)

    # Draw ticks and text values
    ticks = [300, 284, 268, 253, 237, 222, 206, 191, 175, 160, 144, 129, 113, 98, 82, 67, 51, 36, 20, 5]
    if vmax <= 220:
        ticks = [220, 205, 190, 175, 160, 145, 130, 115, 100, 85, 70, 55, 40, 25, 10, 5]

    for val in ticks:
        norm = (val - vmin) / (vmax - vmin) if vmax > vmin else 0
        norm = np.clip(norm, 0, 1)
        y_pos = int((1.0 - norm) * (h - 20)) + 10
        # Text label in white/gray
        txt = str(int(val))
        draw.text((15, y_pos - 5), txt, fill=(255, 255, 255))

    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()


def generate_report_pdf(data: dict, speed: float, canvas_b64: str) -> bytes:
    """
    Generate Tyre Wear Analysis Report PDF matching Image 1 and Image 2 layout.
    """
    buffer = io.BytesIO()
    page_size = portrait(A4)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        topMargin=8*mm,
        bottomMargin=8*mm,
        leftMargin=8*mm,
        rightMargin=8*mm
    )

    elements = []
    styles = getSampleStyleSheet()

    speed_val = int(speed) if speed > 0 else int(data.get("speed", 0))

    # 1. Main Header Title (matching Img 1)
    title_style = ParagraphStyle(
        'MainTitle',
        parent=styles['Heading1'],
        fontSize=20,
        fontName='Helvetica-Bold',
        textColor=black,
        spaceAfter=2,
        alignment=0
    )
    title = Paragraph(f'TYRE WEAR ANALYSIS REPORT – {speed_val}kmph', title_style)
    elements.append(title)

    # Sub-header in Pink
    sub_style = ParagraphStyle(
        'SubTitle',
        parent=styles['Normal'],
        fontSize=10,
        fontName='Helvetica-Bold',
        textColor=HexColor('#D81B60'),
        spaceAfter=6
    )
    subtitle = Paragraph(f'Apollo Tyre Analysis at {speed_val}kmph', sub_style)
    elements.append(subtitle)

    elements.append(Spacer(1, 2*mm))

    # 2. Extract uploaded tyre position & metrics
    pos = data.get("position", "FR").upper()
    best_idx = data.get("best_frame_index", 0)
    best_fn = data.get("best_frame_number", 1)
    frames_dict = data.get("frames", {})
    flat_arr = frames_dict.get(str(best_fn))

    # Get PNG bytes for uploaded tyre
    if canvas_b64 and 'base64,' in canvas_b64:
        try:
            img_data = base64.b64decode(canvas_b64.split('base64,')[1])
            uploaded_png = img_data
        except Exception:
            uploaded_png = _flat_array_to_png(flat_arr) if flat_arr else _create_placeholder_png(pos)
    elif flat_arr:
        uploaded_png = _flat_array_to_png(flat_arr)
    else:
        uploaded_png = _create_placeholder_png(pos)

    placeholder_png = _create_placeholder_png("N/A")

    # Map images to positions
    tyre_images = {}
    for p in ["FL", "FR", "RL", "RR"]:
        if p == pos:
            tyre_images[p] = Image(io.BytesIO(uploaded_png), width=48*mm, height=48*mm)
        else:
            tyre_images[p] = Image(io.BytesIO(placeholder_png), width=48*mm, height=48*mm)

    # Labels for Outer/Inner
    lbl_style = ParagraphStyle('SideLbl', fontName='Helvetica-Bold', fontSize=7, textColor=HexColor('#444444'), alignment=1)

    def make_tyre_box(position_code: str):
        img_item = tyre_images[position_code]
        if position_code in ("FL", "RL"):
            left_lbl = Paragraph("Outer (S1)", lbl_style)
            right_lbl = Paragraph("Inner (S2)", lbl_style)
        else:
            left_lbl = Paragraph("Inner (S2)", lbl_style)
            right_lbl = Paragraph("Outer (S1)", lbl_style)

        box_table = Table(
            [
                [left_lbl, img_item, right_lbl],
                ['', Paragraph(f"<b>{position_code}</b>", ParagraphStyle('PosLbl', fontName='Helvetica-Bold', fontSize=11, alignment=1)), '']
            ],
            colWidths=[14*mm, 50*mm, 14*mm]
        )
        box_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('BACKGROUND', (1, 0), (1, 0), HexColor('#E0F7FA')),
            ('BOX', (0, 0), (-1, -1), 0.8, HexColor('#B0BEC5')),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        return box_table

    # Build 2x2 grid
    fl_box = make_tyre_box("FL")
    fr_box = make_tyre_box("FR")
    rl_box = make_tyre_box("RL")
    rr_box = make_tyre_box("RR")

    banner_style = ParagraphStyle('Banner', fontName='Helvetica-Bold', fontSize=12, textColor=white, alignment=1)
    apollo_banner = Table([[Paragraph("Apollo", banner_style)]], colWidths=[156*mm])
    apollo_banner.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HexColor('#D81B60')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))

    grid_table = Table(
        [
            [apollo_banner, ''],
            [
                Table([[fl_box, fr_box], [rl_box, rr_box]], colWidths=[78*mm, 78*mm]),
                Image(io.BytesIO(_generate_colorbar_png(data.get("vmin", 5), data.get("vmax", 220))), width=22*mm, height=115*mm)
            ]
        ],
        colWidths=[158*mm, 28*mm]
    )
    grid_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))

    elements.append(grid_table)
    elements.append(Spacer(1, 4*mm))

    # 3. Parameters Table (matching Img 2)
    best_m = data.get("best_metrics", {})

    def get_val(key, default="—"):
        val = best_m.get(key)
        if val is None:
            return default
        if isinstance(val, float):
            return f"{val:.1f}" if val % 1 != 0 else f"{int(val)}"
        return str(val)

    # Populate 13 metrics for the uploaded position column
    metrics_map = {
        "Contact area (mm2)":      get_val("contact_area_mm2"),
        "Contact Ratio (%)":       get_val("contact_ratio_pct"),
        "FP Width (mm)":           get_val("fp_width_mm"),
        "FP Length (mm)":          get_val("fp_length_mm"),
        "Outer shoulder S1(mm)":   get_val("outer_shoulder_s1_mm"),
        "Inner Shoulder S2(mm)":   get_val("inner_shoulder_s2_mm"),
        "Shoulder Skewness (%)":   get_val("shoulder_skewness_pct"),
        "Squareness (S1/S2)(%)":   get_val("squareness_s1_s2_pct"),
        "Squareness (Avg)":        get_val("squareness_avg"),
        "Land/sea ratio":          get_val("land_sea_ratio"),
        "Contact pressure":        f"{get_val('contact_pressure_psi')} PSI",
        "Maximum length":          get_val("maximum_length_mm"),
        "Maximum width":           get_val("maximum_width_mm"),
    }

    param_rows = [
        ['Parameters', 'FL', 'FR', 'RL', 'RR']
    ]

    for param_name, param_val in metrics_map.items():
        row = [param_name, '—', '—', '—', '—']
        if pos == 'FL':
            row[1] = param_val
        elif pos == 'FR':
            row[2] = param_val
        elif pos == 'RL':
            row[3] = param_val
        elif pos == 'RR':
            row[4] = param_val
        param_rows.append(row)

    metrics_table = Table(param_rows, colWidths=[70*mm, 28*mm, 28*mm, 28*mm, 28*mm])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#D81B60')),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#B0BEC5')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor('#F5F5F5')]),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))

    elements.append(metrics_table)

    # Build PDF
    doc.build(elements)

    buffer.seek(0)
    return buffer.getvalue()

