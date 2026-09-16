"""Read a journal's real formatting spec out of its own template files.

Guidelines prose is unreliable; a template is the spec. A .docx/.dotx template
states its page size, margins, column count, body font, size and line spacing as
hard values. A LaTeX .cls (or a .zip containing one) states the column count,
class options and bibliography style. Reading these needs no API and no
guessing — it is the most accurate source available offline, so it takes
precedence over anything scraped from the page text.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

EMU_PER_INCH = 914400
TWIPS_PER_INCH = 1440


def _in(v) -> float | None:
    """python-docx Length -> inches."""
    try:
        return round(v.inches, 3)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Word templates
# --------------------------------------------------------------------------- #
def probe_docx(data: bytes) -> dict:
    """Extract a formatting spec from a .docx / .dotx template."""
    from docx import Document
    from docx.oxml.ns import qn

    spec: dict = {"_source": "word template"}
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        return {"_error": f"not a readable Word file ({exc})"}

    # ---- page geometry: use the section with the most body content ---------
    sec = doc.sections[0] if doc.sections else None
    if sec is not None:
        pw, ph = _in(sec.page_width), _in(sec.page_height)
        if pw and ph:
            if abs(pw - 8.27) < 0.15 and abs(ph - 11.69) < 0.2:
                spec["page_size"] = "a4"
            elif abs(pw - 8.5) < 0.15 and abs(ph - 11) < 0.2:
                spec["page_size"] = "letter"
        margins = {"top": _in(sec.top_margin), "bottom": _in(sec.bottom_margin),
                   "left": _in(sec.left_margin), "right": _in(sec.right_margin)}
        if all(v is not None for v in margins.values()):
            spec["margins_in"] = margins
            spec["margin_in"] = margins["top"]

    # ---- column count: max across sections (title pages are often 1-col) ---
    ncols = []
    for s in doc.sections:
        cols = s._sectPr.find(qn("w:cols"))
        if cols is not None:
            n = cols.get(qn("w:num"))
            ncols.append(int(n) if n and n.isdigit() else 1)
    if ncols:
        spec["columns"] = max(ncols)

    # ---- body typography ---------------------------------------------------
    try:
        normal = doc.styles["Normal"]
        if normal.font.name:
            spec["font"] = normal.font.name
        if normal.font.size is not None:
            spec["font_size"] = round(normal.font.size.pt, 1)
        ls = normal.paragraph_format.line_spacing
        if ls:
            spec["line_spacing"] = round(float(ls), 2)
    except Exception:
        pass

    # the Normal style is often left at defaults while the real body text
    # carries direct formatting — fall back to the most common run size
    if "font" not in spec or "font_size" not in spec:
        fonts, sizes = {}, {}
        for p in doc.paragraphs[:400]:
            if len(p.text.strip()) < 40:
                continue
            for r in p.runs:
                if r.font.name:
                    fonts[r.font.name] = fonts.get(r.font.name, 0) + 1
                if r.font.size is not None:
                    pt = round(r.font.size.pt, 1)
                    sizes[pt] = sizes.get(pt, 0) + 1
        if fonts and "font" not in spec:
            spec["font"] = max(fonts, key=fonts.get)
        if sizes and "font_size" not in spec:
            spec["font_size"] = max(sizes, key=sizes.get)

    # ---- heading numbering -------------------------------------------------
    heads = [p.text.strip() for p in doc.paragraphs
             if p.style is not None and (p.style.name or "").lower().startswith("heading")
             and p.text.strip()]
    if not heads:
        heads = [p.text.strip() for p in doc.paragraphs[:200]
                 if 3 < len(p.text.strip()) < 70 and p.runs and p.runs[0].bold]
    roman = sum(1 for h in heads if re.match(r"^[IVXLC]+\.\s", h))
    dec = sum(1 for h in heads if re.match(r"^\d+(\.\d+)*\.?\s", h))
    if roman or dec:
        spec["numbered_sections"] = True
        spec["heading_numbering"] = "roman_upper" if roman > dec else "decimal"

    # ---- caption positions, read from the template's own example ----------
    pos = _caption_positions(doc)
    spec.update(pos)

    # ---- boilerplate the template spells out ------------------------------
    text = "\n".join(p.text for p in doc.paragraphs[:400])
    m = re.search(r"abstract[^.\n]{0,120}?(\d{2,4})\s*words", text, re.I)
    if m and 50 <= int(m.group(1)) <= 1000:
        spec["abstract_max_words"] = int(m.group(1))
    if re.search(r"purpose\s*[-\u2013:]\s*|design/methodology", text, re.I):
        spec["structured_abstract"] = True
    return spec


def _caption_positions(doc) -> dict:
    """Figure/table caption placement, inferred from the template's examples."""
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxPara

    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    seq = []
    for ch in doc.element.body.iterchildren():
        tag = ch.tag.split("}")[-1]
        if tag == "p":
            p = DocxPara(ch, doc)
            seq.append(("img" if ch.findall(".//" + A) else "p", p.text.strip()))
        elif tag == "tbl":
            seq.append(("tbl", ""))

    out = {}
    fig_above = fig_below = tab_above = tab_below = 0
    for i, (kind, txt) in enumerate(seq):
        if kind == "img":
            for d, bucket in ((-1, "a"), (1, "b")):
                j = i + d
                if 0 <= j < len(seq) and re.match(r"^\s*fig", seq[j][1], re.I):
                    if bucket == "a":
                        fig_above += 1
                    else:
                        fig_below += 1
        elif kind == "tbl":
            for d, bucket in ((-1, "a"), (1, "b")):
                j = i + d
                if 0 <= j < len(seq) and re.match(r"^\s*table", seq[j][1], re.I):
                    if bucket == "a":
                        tab_above += 1
                    else:
                        tab_below += 1
    if fig_above or fig_below:
        out["figure_caption_position"] = "above" if fig_above > fig_below else "below"
    if tab_above or tab_below:
        out["table_caption_position"] = "above" if tab_above >= tab_below else "below"
    return out


# --------------------------------------------------------------------------- #
# LaTeX class files
# --------------------------------------------------------------------------- #
def probe_cls(text: str, filename: str = "") -> dict:
    """Extract what a .cls / main .tex tells us about the layout."""
    spec: dict = {"_source": f"latex class {filename}".strip()}
    src = re.sub(r"(?<!\\)%.*", "", text)

    m = re.search(r"\\ProvidesClass\s*\{([^}]+)\}", src)
    if m:
        spec["class_file"] = m.group(1) + ".cls"

    if re.search(r"\\if@twocolumn|\\twocolumn|@twocolumntrue|"
                 r"ExecuteOptions\{[^}]*twocolumn", src):
        spec["columns"] = 2
    elif re.search(r"\\onecolumn|@twocolumnfalse", src):
        spec["columns"] = 1

    if re.search(r"\\paperwidth\s*=?\s*210mm|a4paper", src, re.I):
        spec["page_size"] = "a4"
    elif re.search(r"letterpaper|\\paperwidth\s*=?\s*8\.5in", src, re.I):
        spec["page_size"] = "letter"

    m = re.search(r"\\bibliographystyle\s*\{([^}]+)\}", src)
    if m:
        spec["bib_style"] = m.group(1)

    m = re.search(r"\\documentclass\s*(\[[^\]]*\])?\s*\{([^}]+)\}", src)
    if m:
        spec["documentclass"] = f"\\documentclass{m.group(1) or ''}{{{m.group(2)}}}"
        if m.group(1) and re.search(r"\b(\d{1,2})pt\b", m.group(1)):
            spec["font_size"] = int(re.search(r"\b(\d{1,2})pt\b", m.group(1)).group(1))

    if re.search(r"\\ProvidesClass\s*\{sagej\}|\\corrauth", src):
        spec["author_mode"] = "sage"
    elif re.search(r"\\begin\{IEEEkeywords\}", src):
        spec["author_mode"] = "ieee"
    elif re.search(r"\\begin\{frontmatter\}", src):
        spec["author_mode"] = "elsevier"
    elif re.search(r"\\affil\[|sn-jnl", src):
        spec["author_mode"] = "springer"

    m = re.search(r"margin\s*=\s*(\d+(?:\.\d+)?)\s*(in|cm|mm)", src, re.I)
    if m:
        v, u = float(m.group(1)), m.group(2).lower()
        spec["margin_in"] = round(v if u == "in" else
                                  (v / 2.54 if u == "cm" else v / 25.4), 2)
    return spec


# --------------------------------------------------------------------------- #
def probe(filename: str, data: bytes) -> dict:
    """Dispatch on file type. Handles zipped LaTeX kits."""
    low = filename.lower()
    if low.endswith((".docx", ".dotx")):
        return probe_docx(data)
    if low.endswith((".cls", ".sty", ".tex", ".latex")):
        for enc in ("utf-8", "latin-1"):
            try:
                return probe_cls(data.decode(enc), Path(filename).name)
            except UnicodeDecodeError:
                continue
    if low.endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except Exception as exc:
            return {"_error": f"unreadable zip ({exc})"}
        merged: dict = {}
        names = z.namelist()
        # class file first — it is authoritative for LaTeX
        for n in sorted(names, key=lambda x: (not x.lower().endswith(".cls"), len(x))):
            if n.lower().endswith((".cls", ".tex")):
                try:
                    got = probe_cls(z.read(n).decode("utf-8", "ignore"), Path(n).name)
                except Exception:
                    continue
                cls_stem = (merged.get("class_file") or "")[:-4].lower()
                doc_cls = re.search(r"\{([^}]+)\}\s*$", got.get("documentclass", ""))
                if (cls_stem and doc_cls and doc_cls.group(1).lower() != cls_stem):
                    # a sample .tex for some other class must not override the kit's own
                    got.pop("documentclass", None)
                    got.pop("author_mode", None)
                for k, v in got.items():
                    merged.setdefault(k, v)
                # keep reading: the sample .tex carries \documentclass options
                # and frontmatter commands that the .cls alone does not
        for n in names:
            if n.lower().endswith((".docx", ".dotx")):
                got = probe_docx(z.read(n))
                for k, v in got.items():
                    if not k.startswith("_"):
                        merged.setdefault(k, v)
                break
        merged.setdefault("_source", "zip template kit")
        return merged
    return {"_error": f"no probe for {Path(filename).suffix or 'this file type'}"}


# --------------------------------------------------------------------------- #
# What a template offers, so the user can approve each piece individually
# --------------------------------------------------------------------------- #
def inventory(data: bytes) -> dict:
    """List the reusable furniture in a Word template.

    The point is consent: rather than silently importing (or silently dropping)
    a publisher's logo, running head and page numbers, show what the template
    contains and let the user decide piece by piece.
    """
    import io
    inv = {"logos": 0, "masthead_lines": [], "headers": 0, "footers": 0,
           "page_numbers": False, "styles": 0, "error": ""}
    try:
        from docx import Document
        from docx.text.paragraph import Paragraph as DocxPara
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        inv["error"] = str(exc)
        return inv

    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    body = doc.element.body
    for i, child in enumerate(body.iterchildren()):
        if i > 25:
            break
        if child.tag.split("}")[-1] != "p":
            continue
        if child.findall(".//" + A):
            inv["logos"] += 1
        txt = DocxPara(child, doc).text.strip()
        if txt and len(txt) < 160 and len(inv["masthead_lines"]) < 6:
            inv["masthead_lines"].append(txt)

    for sec in doc.sections:
        for hdr in (sec.header, sec.first_page_header, sec.even_page_header):
            try:
                if hdr is not None and (hdr.paragraphs and
                                        any(p.text.strip() for p in hdr.paragraphs)):
                    inv["headers"] += 1
            except Exception:
                pass
        for ftr in (sec.footer, sec.first_page_footer, sec.even_page_footer):
            try:
                if ftr is None:
                    continue
                if ftr.paragraphs and any(p.text.strip() for p in ftr.paragraphs):
                    inv["footers"] += 1
                xml = ftr._element.xml if hasattr(ftr, "_element") else ""
                if "PAGE" in xml:
                    inv["page_numbers"] = True
            except Exception:
                pass

    try:
        inv["styles"] = len(doc.styles)
    except Exception:
        pass
    return inv
