"""
pdf_export.py – PDF Report Generation
Generates professional tyre analysis reports using ReportLab
"""

import io
import base64
from datetime import datetime
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white, lightgrey
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.pdfgen import canvas
from PIL import Image as PILImage


def generate_report_pdf(data: dict, speed: float, canvas_b64: str) -> bytes:
    """
    Generate a professional PDF report matching the reference format.
    
    Args:
        data: Result dict from processor.load_and_process()
        speed: Vehicle speed in km/h
        canvas_b64: Base64-encoded PNG heatmap from browser canvas
    
    Returns:
        PDF bytes
    """
    buffer = io.BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=page_size, topMargin=10*mm, bottomMargin=10*mm, leftMargin=10*mm, rightMargin=10*mm)
    
    elements = []
    styles = getSampleStyleSheet()
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=black,
        spaceAfter=6,
        alignment=0  # Left align
    )
    
    title = Paragraph('TYRE WEAR ANALYSIS REPORT', title_style)
    elements.append(title)
    
    # Subtitle with speed
    subtitle_style = ParagraphStyle(
        'CustomSubtitle',
        parent=styles['Normal'],
        fontSize=12,
        textColor=HexColor('#C80050'),
        spaceAfter=12
    )
    subtitle = Paragraph(f'Apollo vs JK Comparison at {int(speed)}kmph', subtitle_style)
    elements.append(subtitle)
    
    # Divider
    elements.append(Spacer(1, 2*mm))
    
    # Create comparison table
    best_metrics = data.get('best_metrics', {})
    contact_patch = data.get('contact_patch', {})
    vmin = data.get('vmin', 5)
    vmax = data.get('vmax', 220)
    
    # Decode canvas image if provided
    heatmap_img = None
    if canvas_b64:
        try:
            # Remove data URL prefix if present
            if 'base64,' in canvas_b64:
                canvas_b64 = canvas_b64.split('base64,')[1]
            
            img_data = base64.b64decode(canvas_b64)
            img_buffer = io.BytesIO(img_data)
            heatmap_img = Image(img_buffer, width=70*mm, height=70*mm)
        except Exception as e:
            print(f"Error decoding heatmap image: {e}")
    
    # Build comparison table data
    table_data = []
    
    # Headers row
    if heatmap_img:
        table_data.append([
            Paragraph('<b>Apollo</b>', styles['Normal']),
            Spacer(1, 5*mm),
            Paragraph('<b>JK</b>', styles['Normal'])
        ])
        
        # Heatmap images row
        table_data.append([
            heatmap_img,
            Spacer(1, 5*mm),
            heatmap_img
        ])
    
    # Parameters rows
    width_val = f"{contact_patch.get('width_mm', 0):.1f}"
    length_val = f"{contact_patch.get('length_mm', 0):.1f}"
    s1s2_val = f"{best_metrics.get('avg_pressure_psi', 0):.1f}/{best_metrics.get('peak_pressure_psi', 0):.1f}"
    area_val = f"{best_metrics.get('contact_area_mm2', 0):.0f}"
    
    params = [
        ['Width (mm)', width_val, width_val],
        ['C/L Length (mm)', length_val, length_val],
        ['S1/S2 (mm)', s1s2_val, s1s2_val],
        ['FP Contact area', area_val, area_val]
    ]
    
    table_data.extend(params)
    
    # Create table
    comparison_table = Table(table_data, colWidths=[60*mm, 20*mm, 60*mm])
    comparison_table.setStyle(TableStyle([
        # Header row styling
        ('BACKGROUND', (0, 0), (2, 0), HexColor('#DB2777')),
        ('TEXTCOLOR', (0, 0), (2, 0), white),
        ('ALIGN', (0, 0), (2, 0), 'CENTER'),
        ('FONTNAME', (0, 0), (2, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (2, 0), 11),
        
        # Data rows - alternating background
        ('BACKGROUND', (0, 2), (2, 2), lightgrey),
        ('BACKGROUND', (0, 4), (2, 4), lightgrey),
        ('BACKGROUND', (0, 6), (2, 6), lightgrey),
        
        # All cells borders and alignment
        ('GRID', (0, 0), (-1, -1), 1, black),
        ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 2), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 2), (-1, -1), 4),
    ]))
    
    elements.append(comparison_table)
    elements.append(Spacer(1, 8*mm))
    
    # Frame Analysis Details section
    details_style = ParagraphStyle(
        'DetailsHead',
        parent=styles['Heading2'],
        fontSize=11,
        textColor=HexColor('#5B2D91'),
        spaceAfter=6,
        spaceBefore=6
    )
    
    details_para = Paragraph('Frame Analysis Details', details_style)
    elements.append(details_para)
    
    # Metrics grid
    metrics_data = [
        [
            f"Frame: {best_metrics.get('frame_number', '—')}",
            f"Peak Pressure: {best_metrics.get('peak_pressure_psi', '—')} PSI",
            f"Avg Pressure: {best_metrics.get('avg_pressure_psi', '—')} PSI",
            f"Load: {best_metrics.get('estimated_load_kg', '—')} kg"
        ],
        [
            f"Sensels: {best_metrics.get('active_sensels', '—')}",
            f"Coverage: {best_metrics.get('contact_pct', '—')}%",
            f"Range: {vmin}–{vmax} PSI",
            f"Frames: {len(data.get('frame_numbers', []))}"
        ]
    ]
    
    metrics_table = Table(metrics_data, colWidths=[50*mm, 50*mm, 50*mm, 50*mm])
    metrics_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#CCCCCC')),
    ]))
    
    elements.append(metrics_table)
    
    # Build PDF
    doc.build(elements)
    
    buffer.seek(0)
    return buffer.getvalue()
