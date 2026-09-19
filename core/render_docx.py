"""Render a Manuscript into a .docx matching a journal profile."""
from __future__ import annotations

import io
import os
import re

from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.oxml import OxmlElement, parse_xml

from . import omml as _omml
from . import eqimage as _eqimg
from .model import Manuscript
from . import inline_math as _im
from . import template_roles as _roles

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
         "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]

PAGE = {"a4": (Inches(8.27), Inches(11.69)), "letter": (Inches(8.5), Inches(11))}


def _set_columns(section, n: int):
    cols = section._sectPr.xpath("./w:cols")[0]
    cols.set(qn("w:num"), str(n))
    cols.set(qn("w:space"), "288")  # 0.2"


SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "with", "at", "by", "from"}


def _span_section(doc, ncols: int, pw, m):
    """Insert a continuous section break switching the column count."""
    new = doc.add_section(WD_SECTION.CONTINUOUS)
    new.page_width = pw
    new.page_height = doc.sections[0].page_height
    new.top_margin, new.bottom_margin = Inches(m["top"]), Inches(m["bottom"])
    new.left_margin, new.right_margin = Inches(m["left"]), Inches(m["right"])
    _set_columns(new, ncols)
    return new


def _apply_table_borders(tb, doc) -> None:
    """Give a table visible borders without depending on a named style.

    `tb.style = "Table Grid"` raises KeyError when the document has no style by
    that name — which is the normal case for a publisher template, since Word
    only writes out the styles a template actually uses. Try the built-in names
    that might be present, then fall back to writing the borders directly, which
    always works and looks identical.
    """
    for name in ("Table Grid", "TableGrid", "Table Grid Light", "Grid Table 1 Light"):
        try:
            tb.style = name
            return
        except (KeyError, ValueError):
            continue

    tblPr = tb._tbl.tblPr
    for existing in tblPr.findall(qn("w:tblBorders")):
        tblPr.remove(existing)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")          # 0.5pt
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "000000")
        borders.append(el)
    tblPr.append(borders)


def _tight_cell_margins(tb, twips: int) -> None:
    """Shrink default cell padding so dense tables fit their declared width."""
    tblPr = tb._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(twips if side in ("left", "right") else 0))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblPr.append(mar)


def _col_weights(rows: list[list[str]], ncol: int) -> list[float]:
    """Width proportional to the longest word-ish content, so narrow columns
    (Sl.no, counts) don't get the same space as prose columns."""
    widths = []
    for ci in range(ncol):
        longest = 1
        for r in rows:
            if ci < len(r):
                longest = max(longest, len(r[ci]))
        widths.append(min(longest, 40) ** 0.6)   # damped, else prose dominates
    total = sum(widths) or 1
    return [x / total for x in widths]


def _fix_table_layout(tb, widths: list[int]):
    """LibreOffice/Word ignore cell widths unless the grid + fixed layout are set."""
    tblPr = tb._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    tblW = OxmlElement("w:tblW")
    # widths arrive in EMU; OOXML table widths are twips (1 twip = 635 EMU)
    tw = [int(x) // 635 if int(x) > 20000 else int(x) for x in widths]
    tblW.set(qn("w:w"), str(sum(tw)))
    tblW.set(qn("w:type"), "dxa")
    tblPr.append(tblW)
    grid = tb._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        for gc, wd in zip(grid.findall(qn("w:gridCol")), tw):
            gc.set(qn("w:w"), str(wd))
    for col, wd in zip(tb.columns, widths):
        col.width = wd


MATH_TOKEN = re.compile("\u27eaMATH(\\d+)\u27eb")


def _para_with_math(doc, block, w, para, mode: str = "image"):
    """A body paragraph, with inline equations injected as real Word math."""
    eqs = getattr(block, "inline_eqs", None) or []
    if not eqs or not MATH_TOKEN.search(block.text):
        return para(MATH_TOKEN.sub("", block.text),
                    align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                    space_after=6, indent=0.2)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.first_line_indent = Inches(0.2)

    def _run(txt):
        txt = MATH_TOKEN.sub("", txt or "")
        if not txt:
            return
        r = p.add_run(txt)
        r.font.size = Pt(w["body_size"])
        r.font.name = w["body_font"]

    pos = 0
    for m in MATH_TOKEN.finditer(block.text):
        _run(block.text[pos:m.start()])
        idx = int(m.group(1))
        eq = eqs[idx] if idx < len(eqs) else None
        done = False
        if eq and mode == "image":
            img = _eqimg.render(eq.get("latex", ""), pt=w["body_size"])
            dims = _eqimg.size_inches(img) if img else None
            if img:
                try:
                    r = p.add_run()
                    if dims:
                        r.add_picture(img, width=Inches(min(dims[0], 2.4)),
                                      height=Inches(dims[1] * min(1.0, 2.4 / dims[0])))
                    else:
                        r.add_picture(img)
                    done = True
                except Exception:
                    done = False
        if not done and eq and eq.get("omml"):
            try:
                p._p.append(_omml.ensure_math_font(parse_xml(eq["omml"])))
                done = True
            except Exception:
                done = False
        if not done and eq:
            r = p.add_run(eq.get("latex", ""))
            r.italic = True
            r.font.name = "Cambria Math"
            r.font.size = Pt(w["body_size"])
        pos = m.end()
    _run(block.text[pos:])
    return p


def _add_equation(doc, eq, w, mode: str = "image") -> None:
    """Centred equation with a right-aligned number.

    The original OMML is re-injected where available, so the equation stays a
    real Word equation the author can edit, rather than flattened text. Falls
    back to the LaTeX string only if the XML could not be carried over.
    """
    from docx.enum.text import WD_TAB_ALIGNMENT

    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(4)
    pf.space_after = Pt(4)
    # centre the body, push the number to the right margin
    sec = doc.sections[-1]
    usable = sec.page_width - sec.left_margin - sec.right_margin
    if w["columns"] > 1:
        usable = int(usable * 0.46)
    pf.tab_stops.add_tab_stop(int(usable * 0.5), WD_TAB_ALIGNMENT.CENTER)
    pf.tab_stops.add_tab_stop(int(usable), WD_TAB_ALIGNMENT.RIGHT)

    p.add_run("\t")
    injected = False
    if getattr(eq, "image_path", "") and os.path.exists(eq.image_path):
        try:
            run = p.add_run()
            run.add_picture(eq.image_path,
                            width=Inches(2.9 if w["columns"] > 1 else 4.5))
            injected = True
        except Exception:
            injected = False
    if not injected and mode == "image":
        img = _eqimg.render(eq.latex, pt=w["body_size"] + 1)
        if img:
            dims = _eqimg.size_inches(img)
            try:
                run = p.add_run()
                if dims:
                    max_w = 3.1 if w["columns"] > 1 else 5.5
                    scale = min(1.0, max_w / dims[0]) if dims[0] else 1.0
                    run.add_picture(img, width=Inches(dims[0] * scale),
                                    height=Inches(dims[1] * scale))
                else:
                    run.add_picture(img)
                injected = True
            except Exception:
                injected = False
    if not injected and eq.omml:
        try:
            node = _omml.ensure_math_font(parse_xml(eq.omml))
            p._p.append(node)
            injected = True
        except Exception:
            injected = False
    if not injected:
        r = p.add_run(eq.latex)
        r.italic = True
        r.font.name = "Cambria Math"
        r.font.size = Pt(w["body_size"])
    if eq.number:
        num = p.add_run(f"\t({eq.number})")
        num.font.size = Pt(w["body_size"])
        num.font.name = w["body_font"]


def _case(s: str, mode: str) -> str:
    if mode == "upper":
        return s.upper()
    if mode == "title":
        words = s.split()
        return " ".join(w.capitalize() if (i == 0 or w.lower() not in SMALL) else w.lower()
                        for i, w in enumerate(words))
    if mode == "sentence":
        return s[:1].upper() + s[1:] if s else s
    return s


def _num(idx: int, mode: str) -> str:
    if mode == "roman_upper":
        return f"{ROMAN[idx] if idx < len(ROMAN) else idx}. "
    if mode == "decimal":
        return f"{idx}. "
    return ""


BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
VML_IMG = "{urn:schemas-microsoft-com:vml}imagedata"

# text that marks where the publisher's masthead ends and the author's
# placeholder content begins
_PLACEHOLDER_START = re.compile(
    r"^\s*(title|paper title|abstract|first author|author\s*name|"
    r"insert\s+(your\s+)?title|\[?\s*title\s*\]?|corresponding author|"
    r"keywords?|index terms)\b", re.I)


def _el_text(el) -> str:
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
    return " ".join((t.text or "") for t in el.iter(W)).strip()


_MASTHEAD_TXT = re.compile(
    r"issn|e-?issn|doi|vol(ume)?\.?\s*[\dx]|number\s*[\dx]|pages?\s*[\dx]|"
    r"received|revised|accepted|available online|homepage|journal|"
    r"©|copyright|https?://|www\.", re.I)


def _is_masthead(el, doc) -> bool:
    """Publisher furniture worth keeping: logos, banners, rule lines, DOI bars.

    Mastheads are frequently a *table* (logo | journal name | seal), which the
    old check ignored — the whole banner was deleted.
    """
    tag = el.tag.split("}")[-1]
    if tag not in ("p", "tbl"):
        return False
    has_img = bool(el.findall(".//" + BLIP) or el.findall(".//" + VML_IMG))
    txt = _el_text(el)
    if tag == "tbl":
        return has_img or bool(txt and _MASTHEAD_TXT.search(txt))
    if has_img:
        return True
    if not txt:
        return True
    if _PLACEHOLDER_START.match(txt):
        return False
    if len(txt) < 200 and _MASTHEAD_TXT.search(txt):
        return True
    if len(txt) <= 12 and txt.isupper():   # journal acronym line, e.g. "JJEE"
        return True
    return False


def _template_typography(doc) -> dict:
    """Read the template's own title/body formatting from its example text.

    Many templates leave Normal at defaults and format the example paragraphs
    directly, so the *text* is the real spec (e.g. JJEE: 11pt Bookman body,
    single spaced, 11pt bold title).
    """
    from collections import Counter
    fonts, sizes, spacing = Counter(), Counter(), Counter()
    title = {}
    for p in doc.paragraphs[:300]:
        t = p.text.strip()
        if not t:
            continue
        runs = [r for r in p.runs if r.text.strip()]
        if not title and runs and all(r.bold for r in runs) and 10 < len(t) < 200 \
                and p.alignment == WD_ALIGN_PARAGRAPH.CENTER \
                and not _MASTHEAD_TXT.search(t):
            sz = next((r.font.size.pt for r in runs if r.font.size), None)
            title = {"size": sz, "font": next((r.font.name for r in runs if r.font.name), None)}
        if len(t) < 150:
            continue
        for r in runs:
            if r.font.name:
                fonts[r.font.name] += len(r.text)
            if r.font.size:
                sizes[r.font.size.pt] += len(r.text)
        ls = p.paragraph_format.line_spacing
        if isinstance(ls, float):
            spacing[round(ls, 2)] += 1
        elif ls is None:
            spacing[None] += 1
    out = {"title": title}
    if fonts:
        out["body_font"] = fonts.most_common(1)[0][0]
    if sizes:
        out["body_size"] = sizes.most_common(1)[0][0]
    if spacing:
        out["line_spacing"] = spacing.most_common(1)[0][0]
    return out


def _open_template(template_bytes: bytes, keep_masthead: bool = True):
    """Open the journal's template and clear the body content only.

    Rebuilding from an extracted spec can only approximate a template. Using the
    template file itself keeps its styles, theme fonts, headers, footers, page
    setup and section breaks exactly as the publisher defined them.

    The masthead matters: many journal templates put the logo, journal name,
    ISSN/DOI bar and banner rules in the body rather than the header. Wiping the
    whole body throws those away — the template comes back looking like a blank
    page. So leading masthead elements are preserved, and clearing starts at the
    first placeholder ("Title", "Abstract", "First Author"…).
    """
    from .docx_io import open_document
    doc = open_document(template_bytes)
    doc._tpl_typo = _template_typography(doc)
    body = doc.element.body
    sectPr = body.find(qn("w:sectPr"))

    children = [c for c in body.iterchildren() if c is not sectPr]
    start = 0
    if keep_masthead:
        for i, child in enumerate(children):
            if _is_masthead(child, doc):
                start = i + 1
                continue
            break
        # don't swallow the whole template if nothing looks like a placeholder
        if start >= len(children):
            start = 0
        # trailing blank paragraphs before the content aren't masthead
        from docx.text.paragraph import Paragraph as DocxPara
        while start > 0:
            prev = children[start - 1]
            if prev.tag.split("}")[-1] == "p" and not DocxPara(prev, doc).text.strip() \
                    and not prev.findall(".//" + BLIP):
                start -= 1
            else:
                break

    doc._roles = _roles.collect(doc, children)
    _roles.keep_header_refs(body)
    for child in children[start:]:
        body.remove(child)
    return doc


def _spec_from_doc(doc, prof: dict) -> dict:
    """Layout values read from the live template, falling back to the profile."""
    w = dict(prof["word"])
    try:
        sec = doc.sections[0]
        if sec.page_width and sec.page_height:
            pw, ph = sec.page_width.inches, sec.page_height.inches
            w["page_size"] = "a4" if abs(pw - 8.27) < 0.2 else "letter"
        w["margins_in"] = {"top": sec.top_margin.inches,
                           "bottom": sec.bottom_margin.inches,
                           "left": sec.left_margin.inches,
                           "right": sec.right_margin.inches}
        cols = sec._sectPr.find(qn("w:cols"))
        if cols is not None:
            n = cols.get(qn("w:num"))
            if n and n.isdigit():
                w["columns"] = int(n)
        normal = doc.styles["Normal"]
        if normal.font.name:
            w["body_font"] = normal.font.name
        if normal.font.size is not None:
            w["body_size"] = normal.font.size.pt
        ls = normal.paragraph_format.line_spacing
        w["line_spacing"] = round(ls, 2) if isinstance(ls, float) else 1.0
    except Exception:
        pass
    typo = getattr(doc, "_tpl_typo", {}) or {}
    for k in ("body_font", "body_size"):
        if typo.get(k):
            w[k] = typo[k]
    if "line_spacing" in typo:
        w["line_spacing"] = typo["line_spacing"] or 1.0
    t = typo.get("title") or {}
    if t.get("size"):
        w["title_size"] = t["size"]
    elif w.get("title_size", 0) > w["body_size"] + 4:
        w["title_size"] = w["body_size"] + 2
    return w


def render(ms: Manuscript, prof: dict, equation_mode: str = "image",
           template_bytes: bytes | None = None,
           keep_masthead: bool = True) -> bytes:
    """Format `ms` for `prof`.

    equation_mode: "image" (visible everywhere) or "native" (editable in Word).
    template_bytes: the journal's own .docx/.dotx template. When supplied, the
        manuscript is poured into that file so its styles, headers, footers and
        page setup are preserved verbatim.
    """
    if template_bytes:
        # No silent fallback: a template that cannot be opened must surface as
        # an error so the caller can warn the user (it used to quietly produce
        # a generic-profile file that looked like the template was ignored).
        doc = _open_template(template_bytes, keep_masthead=keep_masthead)
        w = _spec_from_doc(doc, prof)
        prof = {**prof, "word": w}
    else:
        doc = Document()
        w = prof["word"]

    using_template = template_bytes is not None
    # A supplied template may lack any style we expect by name, so every style
    # access here is defensive: a missing style must degrade the formatting, not
    # abort the whole run.
    try:
        st = doc.styles["Normal"]
        st.font.name = w["body_font"]
        st.font.size = Pt(w["body_size"])
        try:
            st.element.rPr.rFonts.set(qn("w:eastAsia"), w["body_font"])
        except AttributeError:
            pass                      # style has no rPr yet
        st.paragraph_format.line_spacing = w["line_spacing"]
        st.paragraph_format.space_after = Pt(0)
    except (KeyError, ValueError):
        pass

    sec = doc.sections[0]
    m = w["margins_in"]
    if using_template:
        # the template already defines the page; leave it alone
        pw, ph = sec.page_width, sec.page_height
        # …but the title block must span the full width, so start at one column
        # and switch to the journal's column count after the front matter.
        if w["columns"] > 1:
            _set_columns(sec, 1)
    else:
        pw, ph = PAGE.get(w["page_size"], PAGE["a4"])
        sec.page_width, sec.page_height = pw, ph
        sec.top_margin, sec.bottom_margin = Inches(m["top"]), Inches(m["bottom"])
        sec.left_margin, sec.right_margin = Inches(m["left"]), Inches(m["right"])

    R = getattr(doc, "_roles", {}) or {}
    spec = R.get("spec", {})

    def para(text="", size=None, bold=False, italic=False, align=None,
             space_before=0, space_after=6, indent=0.0, role=None):
        if role and R.get(role):
            p = doc.add_paragraph()
            p.add_run(text)
            _roles.skin(p, R[role])
            return p
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold, r.italic = bold, italic
        r.font.size = Pt(size or w["body_size"])
        r.font.name = w["body_font"]
        if align:
            p.alignment = align
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after = Pt(space_after)
        if indent:
            p.paragraph_format.first_line_indent = Inches(indent)
        return p

    # ---- title block (single column even in 2-col layouts) ----------------
    para(ms.title, size=w["title_size"], bold=True,
         align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10, role="title")
    if ms.authors:
        para(", ".join(a.name for a in ms.authors), size=w["body_size"] + 1,
             align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, role="authors")
        affs = list(dict.fromkeys(a.affiliation for a in ms.authors if a.affiliation))
        for af in affs[:4]:
            para(af, size=w["body_size"] - 1, italic=True,
                 align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2, role="affil")
        emails = [a.email for a in ms.authors if a.email]
        if emails:
            para("E-mail: " + ", ".join(emails), size=w["body_size"] - 1,
                 align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10,
                 role="email" if R.get("email") else "affil")

    if w["columns"] > 1:
        new = doc.add_section(WD_SECTION.CONTINUOUS)
        new.page_width, new.page_height = pw, ph
        new.top_margin, new.bottom_margin = Inches(m["top"]), Inches(m["bottom"])
        new.left_margin, new.right_margin = Inches(m["left"]), Inches(m["right"])
        _set_columns(new, w["columns"])

    # ---- abstract ---------------------------------------------------------
    ab_type = prof.get("abstract", {}).get("type", "plain")
    kw_sep = "; " if R.get("abs_tbl") is not None else ", "
    if R.get("abs_tbl") is not None and (ms.abstract or ms.structured_abstract):
        doc.add_paragraph()
        tbl = __import__("copy").deepcopy(R["abs_tbl"])
        abs_txt = ms.abstract or " ".join(f"{k}: {v}" for k, v in ms.structured_abstract.items())
        kws = kw_sep.join(_case(k, "sentence") for k in ms.keywords)
        _roles.fill_abstract_table(tbl, doc, abs_txt, kws + ("." if kws else ""))
        doc.paragraphs[-1]._p.addnext(tbl)
        doc.add_paragraph()
    elif ab_type == "structured" and ms.structured_abstract:
        for k, v in ms.structured_abstract.items():
            p = doc.add_paragraph()
            r = p.add_run(f"{k} – ")
            r.bold = True
            r.font.size = Pt(w["body_size"])
            r2 = p.add_run(v)
            r2.font.size = Pt(w["body_size"])
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    elif ms.abstract:
        p = doc.add_paragraph()
        r = p.add_run("Abstract— " if prof["publisher"] == "IEEE" else "Abstract: ")
        r.bold = True
        r.font.size = Pt(w["body_size"])
        r2 = p.add_run(ms.abstract)
        r2.font.size = Pt(w["body_size"])
        r2.italic = prof["publisher"] == "IEEE"
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    if ms.keywords and R.get("abs_tbl") is None:
        label = "Index Terms— " if prof["publisher"] == "IEEE" else "Keywords: "
        kws = ", ".join(_case(k, prof.get("keywords", {}).get("case", "lower"))
                        for k in ms.keywords)
        p = doc.add_paragraph()
        r = p.add_run(label); r.bold = True; r.font.size = Pt(w["body_size"])
        r2 = p.add_run(kws); r2.font.size = Pt(w["body_size"])
        p.paragraph_format.space_after = Pt(10)

    # ---- body -------------------------------------------------------------
    figs = {f.label: f for f in ms.figures}
    tabs = {t.label: t for t in ms.tables}
    eqs = {e.label: e for e in ms.equations}
    top_idx = 0
    _REF = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?(references?|bibliography|literature cited)\s*$", re.I)

    def _strip_refs(secs):
        out = []
        for sc in secs:
            if _REF.match(sc.title or ""):
                if not ms.references:
                    from .model import Reference
                    for bk in sc.blocks:
                        if bk.kind == "para" and bk.text.strip():
                            ms.references.append(Reference(raw=bk.text.strip()))
                continue
            out.append(sc)
        return out
    try:
        ms.sections = _strip_refs(ms.sections)
    except Exception:
        pass
    for s in ms.sections:
        if s.level == 1:
            top_idx += 1
            title = re.sub(r"^\s*(\d+(\.\d+)*|[IVXLC]+)\.?\s+", "", s.title)
            if R.get("h1"):
                h = R["h1"]
                txt = title.upper() if h["upper"] else _case(title, w["heading_case"])
                if not h["numbered"]:
                    txt = _num(top_idx, w["heading_numbering"]) + txt
                para(txt, role="h1")
                sub_idx = 0
                goto_blocks = True
            else:
                goto_blocks = False
        if s.level == 1 and not goto_blocks:
            heading = _num(top_idx, w["heading_numbering"]) + _case(s.title, w["heading_case"])
            para(heading, size=w["body_size"] + 1, bold=True,
                 align=WD_ALIGN_PARAGRAPH.CENTER if prof["publisher"] == "IEEE" else None,
                 space_before=10, space_after=4)
        elif s.level > 1:
            role = "h2" if s.level == 2 else ("h3" if R.get("h3") else "h2")
            title = re.sub(r"^\s*\d+(\.\d+)+\.?\s+", "", s.title)
            if R.get(role) and not R[role]["numbered"]:
                title = s.title
            para(_case(title, "sentence"), bold=True, italic=True,
                 space_before=6, space_after=3, role=role)

        for b in s.blocks:
            if b.kind == "para":
                p = _para_with_math(doc, b, w, para, equation_mode)
                if R.get("body") and p is not None:
                    _roles.skin(p, R["body"], keep_run_style=True)
            elif b.kind == "equation" and b.ref in eqs:
                _add_equation(doc, eqs[b.ref], w, equation_mode)
            elif b.kind == "figure" and b.ref in figs:
                f = figs[b.ref]
                if f.path and os.path.exists(f.path):
                    try:
                        doc.add_picture(f.path, width=Inches(3.2 if w["columns"] > 1 else 5.5))
                        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                    except Exception:
                        pass
                n = "".join(ch for ch in f.label if ch.isdigit()) or "1"
                fcap = _im.to_plain(f.caption)
                cap_txt = f"Fig. {n}. {fcap}" if fcap else f"Fig. {n}."
                para(cap_txt, size=w["body_size"] - 1,
                     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=8, role="figcap")
            elif b.kind == "table" and b.ref in tabs:
                t = tabs[b.ref]
                n = "".join(ch for ch in t.label if ch.isdigit()) or "1"
                ncol = max((len(r) for r in t.rows), default=0)
                # A table with many columns is unreadable inside a narrow
                # journal column, so span the full page width instead.
                wide = w["columns"] > 1 and ncol >= 4
                if wide:
                    _span_section(doc, 1, pw, m)

                if R.get("tabcap"):
                    cap = _im.to_plain(t.caption) if t.caption else ""
                    para(f"Table {n}. {cap}".strip(), role="tabcap")
                else:
                    para(f"TABLE {n}", size=w["body_size"] - 1, bold=True,
                         align=WD_ALIGN_PARAGRAPH.CENTER, space_before=6, space_after=0)
                if t.caption and not R.get("tabcap"):
                    para(_im.to_plain(t.caption), size=w["body_size"] - 1,
                         align=WD_ALIGN_PARAGRAPH.CENTER, space_after=3)
                if t.rows:
                    tb = doc.add_table(rows=0, cols=ncol)
                    if not R.get("tbl"):
                        _apply_table_borders(tb, doc)
                    tb.autofit = False
                    avail = int(pw - sec.left_margin - sec.right_margin)
                    if not wide and w["columns"] > 1:
                        avail = int(avail * 0.46)
                    # Word adds cell padding on top of the declared width, so
                    # aim slightly under the text width; then enforce a minimum
                    # per column and rescale, otherwise the floor pushes the
                    # total past the margin and the table is cropped.
                    target = int(avail * 0.985)
                    widths = [max(int(target * f), 300)
                              for f in _col_weights(t.rows, ncol)]
                    total = sum(widths)
                    if total > target:
                        scale = target / total
                        widths = [max(int(x * scale), 240) for x in widths]
                        drift = target - sum(widths)
                        if drift:
                            widths[widths.index(max(widths))] += drift
                    # shrink type for dense tables so cells stop wrapping
                    fsize = (spec.get("tables") or {}).get("size") or (w["body_size"] - 2)
                    if ncol >= 6:
                        fsize = w["body_size"] - 3.5
                    elif ncol >= 4:
                        fsize = w["body_size"] - 3
                    for ri, row in enumerate(t.rows):
                        cells = tb.add_row().cells
                        for ci in range(ncol):
                            txt = _im.to_plain(row[ci]) if ci < len(row) else ""
                            cells[ci].text = txt
                            cells[ci].width = widths[ci]
                            for pp in cells[ci].paragraphs:
                                pp.paragraph_format.space_after = Pt(0)
                                for rr in pp.runs:
                                    rr.font.size = Pt(max(fsize, 6))
                                    rr.font.name = w["body_font"]
                                    rr.bold = (ri == 0)
                    if ncol >= 4:
                        _tight_cell_margins(tb, 36)   # 0.025" each side
                    _fix_table_layout(tb, widths)
                    if R.get("tbl"):
                        tsz = (spec.get("tables") or {}).get("size") or (w["body_size"] - 1)
                        if ncol >= 12:
                            tsz = min(tsz, 7.5)
                        elif ncol >= 10:
                            tsz = min(tsz, 8)
                        elif ncol >= 7:
                            tsz = min(tsz, 9)
                        _roles.style_table(tb, R["tbl"], tsz,
                                           (spec.get("tables") or {}).get("font") or w["body_font"])
                para("", space_after=8)
                if wide:
                    _span_section(doc, w["columns"], pw, m)

    # ---- references -------------------------------------------------------
    if ms.references:
        top_idx += 1
        para("REFERENCES" if (R.get("refhead") or {}).get("upper")
             else _case("References", w["heading_case"]), size=w["body_size"] + 1, bold=True,
             align=WD_ALIGN_PARAGRAPH.CENTER if prof["publisher"] == "IEEE" else None,
             space_before=10, space_after=4, role="refhead")
        ref_size = (spec.get("references") or {}).get("size")
        numeric = prof["reference_style"] in ("ieee", "elsevier-numeric",
                                              "multidisciplinary-digital-publishing-institute")
        tab = (R.get("ref") or {}).get("tab")
        for i, r in enumerate(ms.references, 1):
            raw = re.sub(r"^\s*\[?\d+[\].)]\s*", "", r.raw) if numeric else r.raw
            txt = f"[{i}]" + ("\t" if tab else " ") + raw if numeric else raw
            p = para(txt, size=ref_size or (w["body_size"] - 1),
                     align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=3, role="ref")
            if not numeric:
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.first_line_indent = Inches(-0.5)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
