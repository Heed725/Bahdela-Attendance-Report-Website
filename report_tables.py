"""Shared Word, PDF and Excel table exporters for attendance reports."""
from io import BytesIO
from xml.sax.saxutils import escape


def column_weights(columns):
    return [4 if name == 'Employee Name' else 3.2 if name == 'Missing Clockings'
            else 1.6 if name == 'Incomplete' else 1.45 if name == 'Status' else 1.2 for name in columns]


def export_docx(title, subtitle, tables, logo_path=None, *, context_lines=()):
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.enum.table import WD_ALIGN_VERTICAL
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(29.7), Cm(21)
    section.left_margin = section.right_margin = Cm(1.2)
    section.top_margin = section.bottom_margin = Cm(1.2)
    normal = document.styles['Normal']
    normal.font.name, normal.font.size = 'Arial', Pt(9)
    normal.paragraph_format.space_after = Pt(2)
    for name in ['Title', 'Heading 1', 'Heading 2']:
        style = document.styles[name]
        style.font.name = 'Arial'
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(20 if name == 'Title' else 11)
        for border in list(style.element.iter(qn('w:pBdr'))):
            border.getparent().remove(border)
    if logo_path:
        document.add_picture(logo_path, width=Cm(3))
    document.add_heading(title, 0)
    document.add_paragraph(subtitle)
    for line in context_lines:
        document.add_paragraph(line)
    for heading, columns, rows in tables:
        document.add_heading(heading, 2)
        table = document.add_table(rows=1, cols=len(columns))
        table.style = 'Table Grid'
        table.autofit = False
        borders = OxmlElement('w:tblBorders')
        for edge in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
            line = OxmlElement('w:' + edge)
            for key, value in [('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')]:
                line.set(qn('w:' + key), value)
            borders.append(line)
        table._tbl.tblPr.append(borders)
        weights = column_weights(columns)
        widths = [27.3 * weight / sum(weights) for weight in weights]
        for column, width in zip(table.columns, widths):
            column.width = Cm(width)
        repeat = OxmlElement('w:tblHeader')
        table.rows[0]._tr.get_or_add_trPr().append(repeat)
        for cell, label in zip(table.rows[0].cells, columns):
            cell.text = label
            shading = OxmlElement('w:shd')
            shading.set(qn('w:fill'), '8B0000')
            cell._tc.get_or_add_tcPr().append(shading)
            for run in cell.paragraphs[0].runs:
                run.bold = True
                run.font.color.rgb = RGBColor.from_string('FFF3CD')
        for record in rows:
            cells = table.add_row().cells
            for cell, label in zip(cells, columns):
                cell.text = str(record.get(label, ''))
            background = 'EEEEEE' if record.get('Status') == 'Absent' else 'FFF3CD' if record.get('Status') == 'Incomplete' else None
            if background:
                for cell in cells:
                    shading = OxmlElement('w:shd')
                    shading.set(qn('w:fill'), background)
                    cell._tc.get_or_add_tcPr().append(shading)
        for row in table.rows:
            row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
            for cell, label, width in zip(row.cells, columns, widths):
                cell.width = Cm(width)
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                margins = OxmlElement('w:tcMar')
                for edge in ['top', 'bottom', 'left', 'right']:
                    margin = OxmlElement('w:' + edge)
                    margin.set(qn('w:w'), '60')
                    margin.set(qn('w:type'), 'dxa')
                    margins.append(margin)
                cell._tc.get_or_add_tcPr().append(margins)
                for paragraph in cell.paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT if label in ['Employee Name', 'Missing Clockings'] else WD_ALIGN_PARAGRAPH.CENTER
        document.add_paragraph()
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def export_pdf(title, subtitle, tables, logo_path=None, *, context_lines=()):
    from pathlib import Path
    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=12 * mm,
                                rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm)
    font_directory = Path(reportlab.__file__).parent / 'fonts'
    for name, filename in [('LunchSans', 'Vera.ttf'), ('LunchSansBold', 'VeraBd.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_directory / filename)))
    body = ParagraphStyle('Body', fontName='LunchSans', fontSize=8, leading=11)
    heading_style = ParagraphStyle('Heading', parent=body, fontName='LunchSansBold', fontSize=11, leading=14, textColor=colors.HexColor('#8B0000'))
    header = ParagraphStyle('Header', parent=body, fontName='LunchSansBold', textColor=colors.HexColor('#FFF3CD'))
    story = []
    if logo_path:
        logo = Image(logo_path)
        ratio = 75 / logo.imageWidth
        logo.drawWidth, logo.drawHeight = 75, logo.imageHeight * ratio
        story.append(logo)
    context = context_lines
    for text in [title, subtitle, *context]:
        story.extend([Paragraph(escape(text), heading_style if text == title else body), Spacer(1, 5)])
    for heading, columns, rows in tables:
        story.extend([Spacer(1, 8), Paragraph(escape(heading), heading_style), Spacer(1, 4)])
        data = [[Paragraph(escape(label), header) for label in columns]]
        data.extend([[Paragraph(escape(str(record.get(label, ''))), body) for label in columns] for record in rows])
        weights = column_weights(columns)
        table = Table(data, colWidths=[document.width * weight / sum(weights) for weight in weights], repeatRows=1)
        style = [('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8B0000')),
                 ('GRID', (0, 0), (-1, -1), .4, colors.HexColor('#D4A017')),
                 ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                 ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]
        for index, record in enumerate(rows, 1):
            background = '#EEEEEE' if record.get('Status') == 'Absent' else '#FFF3CD' if record.get('Status') == 'Incomplete' else '#FFFFFF'
            style.append(('BACKGROUND', (0, index), (-1, index), colors.HexColor(background)))
        table.setStyle(TableStyle(style))
        story.append(table)
    document.build(story)
    return output.getvalue()


def export_xlsx(title, subtitle, tables, logo_path=None, *, context_lines=(), sheet_name='Report'):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    count = len(tables[0][1]) if tables else 1
    context = context_lines
    for index, text in enumerate([title, subtitle, *context], 1):
        sheet.merge_cells(start_row=index, start_column=1, end_row=index, end_column=count)
        cell = sheet.cell(index, 1, text)
        cell.font = Font(name='Arial', bold=index == 1, size=14 if index == 1 else 10, color='8B0000')
        cell.alignment = Alignment(vertical='center', wrap_text=True)
        sheet.row_dimensions[index].height = 25
    border = Border(*(Side(style='thin', color='D4A017') for _ in range(4)))
    current = 6
    for heading, columns, rows in tables:
        sheet.merge_cells(start_row=current, start_column=1, end_row=current, end_column=len(columns))
        cell = sheet.cell(current, 1, heading)
        cell.font = Font(name='Arial', bold=True, color='8B0000')
        current += 1
        for index, label in enumerate(columns, 1):
            cell = sheet.cell(current, index, label)
            cell.font = Font(name='Arial', bold=True, color='FFF3CD')
            cell.fill = PatternFill('solid', fgColor='8B0000')
            cell.alignment = Alignment(wrap_text=True, vertical='center')
        sheet.row_dimensions[current].height = 30
        current += 1
        for record in rows:
            for index, label in enumerate(columns, 1):
                value = record.get(label, '')
                # Force names/IDs to text; terminal fields must never become formulas.
                cell = sheet.cell(current, index, value)
                if isinstance(value, str):
                    cell.data_type = 's'
                cell.font = Font(name='Arial', size=10)
                cell.alignment = Alignment(wrap_text=True, vertical='center')
                cell.border = border
                fill = 'EEEEEE' if record.get('Status') == 'Absent' else 'FFF3CD' if record.get('Status') == 'Incomplete' else 'FFFFFF'
                cell.fill = PatternFill('solid', fgColor=fill)
            sheet.row_dimensions[current].height = 30
            current += 1
        for index, label in enumerate(columns, 1):
            sheet.column_dimensions[get_column_letter(index)].width = 32 if label == 'Employee Name' else 35 if label == 'Missing Clockings' else 14
        current += 1
    sheet.freeze_panes = 'D8'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


