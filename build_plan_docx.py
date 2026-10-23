# -*- coding: utf-8 -*-
from pathlib import Path
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(r"D:\AIdirector")
SRC = ROOT / "DEVELOPMENT_PLAN.md"
OUT = ROOT / "Yuzi_CS2_AIDirector_开发计划书.docx"
NAVY = RGBColor(0x14, 0x21, 0x3D)
GOLD = RGBColor(0xB8, 0x8A, 0x2E)
GRAY = RGBColor(0x55, 0x5B, 0x66)
LIGHT = RGBColor(0x6B, 0x72, 0x80)


def ea(run, name="Microsoft YaHei"):
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:eastAsia"), name)
    rf.set(qn("w:ascii"), name)
    rf.set(qn("w:hAnsi"), name)


def shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def page_number(par):
    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = par.add_run("第 ")
    r.font.size = Pt(9)
    r.font.color.rgb = LIGHT
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "18")
    rpr.append(sz)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = "1"
    run.append(t)
    fld.append(run)
    par._p.append(fld)
    r2 = par.add_run(" 页")
    r2.font.size = Pt(9)
    r2.font.color.rgb = LIGHT


def clean(text):
    text = text.replace("**", "")
    text = text.replace(chr(96), "")
    text = text.replace("[x]", "√").replace("[X]", "√").replace("[ ]", "□")
    if "](" in text and text.startswith("[") and text.endswith(")"):
        try:
            label, url = text[1:-1].split("](", 1)
            return label + "（" + url + "）"
        except Exception:
            pass
    return text.strip()


def table_text(text):
    text = text.strip()
    if text.startswith("[") and "](" in text and text.endswith(")"):
        return text[1:].split("](", 1)[0]
    return clean(text)


def inline(par, text, size=10.5, color=GRAY):
    text = clean(text)
    if not text:
        return
    r = par.add_run(text)
    r.font.size = Pt(size)
    r.font.color.rgb = color
    ea(r)


def add_table(doc, rows):
    if not rows:
        return
    cols = len(rows[0])
    t = doc.add_table(rows=1, cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, text in enumerate(rows[0]):
        c = t.rows[0].cells[i]
        c.text = ""
        p = c.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(table_text(text))
        r.bold = True
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(255, 255, 255)
        ea(r)
        shade(c, "14213D")
    for idx, row in enumerate(rows[1:]):
        cells = t.add_row().cells
        for i in range(cols):
            c = cells[i]
            c.text = ""
            p = c.paragraphs[0]
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(1)
            inline(p, table_text(row[i] if i < len(row) else ""), 8.8,
                   RGBColor(0x33, 0x3A, 0x45))
            if idx % 2:
                shade(c, "F7F8FA")
    # 表头跨页重复，行内禁止断开
    trPr = t.rows[0]._tr.get_or_add_trPr()
    th = OxmlElement("w:tblHeader")
    th.set(qn("w:val"), "true")
    trPr.append(th)
    for row in t.rows:
        trPr = row._tr.get_or_add_trPr()
        cs = OxmlElement("w:cantSplit")
        trPr.append(cs)
    # 固定列宽，避免 URL/长文本把列挤成竖排
    width_map = {1: [6.5], 2: [1.45, 5.05], 3: [1.35, 1.9, 3.25],
                 4: [0.9, 1.1, 2.6, 1.9], 5: [0.85, 0.95, 1.55, 1.55, 1.6]}
    widths = width_map.get(cols, [6.5 / cols] * cols)
    for i, w in enumerate(widths):
        t.columns[i].width = Inches(w)
        for c in t.columns[i].cells:
            c.width = Inches(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_code(doc, lines):
    t = doc.add_table(rows=1, cols=1)
    c = t.cell(0, 0)
    shade(c, "F2F4F7")
    c.width = Inches(6.5)
    p = c.paragraphs[0]
    for i, line in enumerate(lines):
        if i:
            p = c.add_paragraph()
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.line_spacing = 1.1
        r = p.add_run(line if line else " ")
        r.font.name = "Consolas"
        r.font.size = Pt(8.5)
        r.font.color.rgb = RGBColor(0x33, 0x3A, 0x45)
        ea(r, "Consolas")
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def main():
    lines = SRC.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## 0.")), 0)
    heads = [l[3:].strip() for l in lines[start:] if l.startswith("## ")]

    doc = Document()
    doc.core_properties.title = "Yuzi CS2 AI Director 开发计划书"
    doc.core_properties.author = "Yuzi CS2 AI Director"
    doc.core_properties.subject = "开发计划、模块进度与下一阶段路线图"

    sec = doc.sections[0]
    sec.top_margin = Inches(0.8)
    sec.bottom_margin = Inches(0.75)
    sec.left_margin = Inches(0.9)
    sec.right_margin = Inches(0.9)
    sec.different_first_page_header_footer = True

    n = doc.styles["Normal"]
    n.font.name = "Microsoft YaHei"
    n.font.size = Pt(10.5)
    n.font.color.rgb = GRAY
    n.paragraph_format.line_spacing = 1.35
    n.paragraph_format.space_after = Pt(4)
    n.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    for name, size in (("Heading 1", 17), ("Heading 2", 13), ("Heading 3", 11.5)):
        st = doc.styles[name]
        st.font.name = "Microsoft YaHei"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = NAVY
        st.paragraph_format.space_before = Pt(12 if name == "Heading 1" else 9)
        st.paragraph_format.space_after = Pt(5)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(24)
    logo = ROOT / "assets" / "yuzi_logo_cover.png"
    if logo.exists():
        p.add_run().add_picture(str(logo), width=Inches(1.8))
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(16)
    r = p.add_run("YUZI CUP  ·  DIRECTOR")
    r.font.name = "Consolas"
    r.font.size = Pt(10)
    r.font.color.rgb = GOLD
    r.bold = True
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(10)
    r = p.add_run("CS2 AI 导播系统")
    r.font.size = Pt(23)
    r.bold = True
    r.font.color.rgb = NAVY
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("开发计划书")
    r.font.size = Pt(34)
    r.bold = True
    r.font.color.rgb = NAVY
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    r = p.add_run("Development Plan & Progress Review")
    r.font.size = Pt(10.5)
    r.italic = True
    r.font.color.rgb = LIGHT
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(18)
    r = p.add_run("主程序 v1.10.17    ·    训练器 v1.1.8")
    r.font.size = Pt(11)
    r.font.color.rgb = GRAY
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("2026-10-23")
    r.font.size = Pt(11)
    r.font.color.rgb = GRAY
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(24)
    r = p.add_run("内部开发文档  ·  工作区 D:\\AIdirector")
    r.font.size = Pt(8.5)
    r.font.color.rgb = LIGHT
    doc.add_page_break()

    p = doc.add_paragraph()
    r = p.add_run("目录")
    r.font.size = Pt(18)
    r.bold = True
    r.font.color.rgb = NAVY
    p.paragraph_format.space_after = Pt(8)
    for h in heads:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.15)
        p.paragraph_format.space_after = Pt(2)
        inline(p, h, 9.8)
    doc.add_page_break()

    i = start
    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()
        if not s or s == "---":
            i += 1
            continue
        if s.startswith(chr(96) * 3):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith(chr(96) * 3):
                buf.append(lines[i].rstrip())
                i += 1
            i += 1
            add_code(doc, buf)
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                sep = True
                for c in cells:
                    if c:
                        core = c.strip()
                        if not (core.startswith("---") or core.startswith(":--") or core.endswith("--:")):
                            sep = False
                if not sep:
                    rows.append(cells)
                i += 1
            add_table(doc, rows)
            continue
        if s.startswith("#### "):
            p = doc.add_paragraph(style="Heading 3")
            p.add_run(clean(s[5:]))
            i += 1
            continue
        if s.startswith("### "):
            p = doc.add_paragraph(style="Heading 2")
            p.add_run(clean(s[4:]))
            i += 1
            continue
        if s.startswith("## "):
            p = doc.add_paragraph(style="Heading 1")
            p.add_run(clean(s[3:]))
            i += 1
            continue
        if s.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.25)
            inline(p, s[2:], 9.8)
            i += 1
            continue
        if line.lstrip().startswith("- ") or line.lstrip().startswith("* "):
            p = doc.add_paragraph(style="List Bullet")
            indent = (len(line) - len(line.lstrip())) // 2
            p.paragraph_format.left_indent = Inches(0.25 + 0.2 * indent)
            p.paragraph_format.space_after = Pt(2)
            inline(p, line.lstrip()[2:], 10.2)
            i += 1
            continue
        stripped = line.lstrip()
        if len(stripped) > 2 and stripped[0].isdigit() and ". " in stripped:
            num, body = stripped.split(". ", 1)
            p = doc.add_paragraph()
            indent = (len(line) - len(stripped)) // 4
            p.paragraph_format.left_indent = Inches(0.3 + 0.2 * indent)
            p.paragraph_format.first_line_indent = Inches(-0.2)
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(num + ". ")
            r.font.size = Pt(10.2)
            r.font.color.rgb = GRAY
            ea(r)
            inline(p, body, 10.2)
            i += 1
            continue
        p = doc.add_paragraph()
        inline(p, s, 10.4)
        i += 1

    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = hp.add_run("Yuzi CS2 AI Director  ·  开发计划书")
    r.font.size = Pt(8)
    r.font.color.rgb = LIGHT
    ea(r)
    page_number(sec.footer.paragraphs[0])

    doc.save(OUT)
    print("saved", OUT)
    print("size", OUT.stat().st_size)
    print("paragraphs", len(doc.paragraphs), "tables", len(doc.tables))


if __name__ == "__main__":
    main()
