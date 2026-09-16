"""Parse an uploaded manuscript (.docx, .tex/.zip, .pdf) into a Manuscript.

Heuristics matter more than cleverness here: real client manuscripts rarely use
proper Word heading styles, so we fall back to pattern matching on numbering
and known section names.
"""
from __future__ import annotations

import re
import os
import zipfile
import tempfile
from pathlib import Path

from .model import (Manuscript, Author, Section, Block, Figure, Table,
                     Reference, Equation)
from . import omml
from . import inline_math

CANONICAL_HEADINGS = [
    "abstract", "keywords", "introduction", "background", "related work",
    "literature review", "materials and methods", "methodology", "method",
    "proposed method", "proposed methodology", "system model", "experimental setup",
    "results", "results and discussion", "discussion", "conclusion", "conclusions",
    "conclusion and future work", "future work", "acknowledgment", "acknowledgement",
    "acknowledgments", "references", "appendix", "data availability",
    "data availability statement", "data and code availability",
    "conflicts of interest", "conflict of interest", "declaration of competing interest",
    "funding", "author contributions", "ethics statement", "ethical statement",
]

NUMBERED_HEADING = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)*)\.?|([IVXLC]+)\.)\s+([A-Z][^.]{2,80})\s*$"
)
FIG_MARK = re.compile(r"^\s*fig(?:ure)?\s*\.?\s*(\d+)\s*[.:\)]?\s*(.*)$", re.I)
TAB_MARK = re.compile(r"^\s*table\s*\.?\s*([IVXLCM]+|\d+)\s*[.:\)]?\s*(.*)$", re.I)
# kept for the PDF/LaTeX paths
FIG_CAPTION = FIG_MARK
TAB_CAPTION = TAB_MARK
ROMAN_MAP = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
             "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13,
             "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18,
             "XIX": 19, "XX": 20}


def _clean(t: str) -> str:
    """Normalise the whitespace Word litters through pasted text."""
    if not t:
        return ""
    t = t.replace("\xa0", " ").replace("\u2009", " ").replace("\t", " ")
    # strip control characters Word leaves behind (field codes, bookmarks)
    t = "".join(c for c in t if ord(c) >= 32 or c == "\n")
    return re.sub(r"[ ]{2,}", " ", t).strip()


_TYPED_EQ_NUM = re.compile(
    r"(?:\\q?quad|#|\\,|\s)*(?:\\left\s*\(\s*(\d{1,3}[a-z]?)\s*\\right\s*\)"
    r"|\(\s*(\d{1,3}[a-z]?)\s*\))\s*$")


def _split_typed_number(tex: str) -> tuple[str, str | None]:
    """Pull an equation number typed *inside* the equation ("... #(25)").

    Left in, the renderer adds its own number and the paper shows "(25) (25)".
    Only a number after a separator (tab/#/quad) or at the very end of a
    non-trivial expression is taken, so a genuine "(2)" factor is not eaten.
    """
    m = _TYPED_EQ_NUM.search(tex or "")
    if not m or m.start() == 0:
        return tex, None
    head = tex[:m.start()].rstrip()
    sep = tex[m.start():m.end()]
    if not (re.search(r"\\q?quad|#|\s", sep) or "\\left" in sep):
        return tex, None                  # "b(2)" is maths, not a number
    if head.endswith(("+", "-", "=", "\\cdot", "\\times")):
        return tex, None
    if len(head) < 3:
        return tex, None
    return head.rstrip(" ,\\"), (m.group(1) or m.group(2))


def _numval(tok: str) -> int | None:
    tok = tok.strip().upper()
    if tok.isdigit():
        return int(tok)
    return ROMAN_MAP.get(tok)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:\w]+", re.I)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _heading_level(text: str) -> int | None:
    t = text.strip()
    if not t or len(t) > 120:
        return None
    m = NUMBERED_HEADING.match(t)
    if m:
        if m.group(1):
            return m.group(1).count(".") + 1
        return 1
    low = t.lower().rstrip(":").strip()
    if low in CANONICAL_HEADINGS:
        return 1
    # Unnumbered ALL-CAPS heading — very common in client manuscripts.
    if _is_allcaps_heading(t):
        return 2
    return None


def _style_heading_level(style: str) -> int | None:
    """Level from a Word heading style ("Heading 2", "heading2", "Überschrift 1")."""
    st = (style or "").strip().lower()
    m = re.match(r"^(?:heading|überschrift|titre|título|titolo)\s*(\d)$", st)
    if m:
        return max(1, min(3, int(m.group(1))))
    return None


def _is_body_mention(text: str, m) -> bool:
    """True when "Figure 2 ..." / "Table 3 ..." is a sentence, not a caption.

    Captions separate the number from the title ("Fig. 1. Title", "Table 2:
    Title", "TABLE 3") or start the title with a capital ("Table 1 Nested
    cases"). A mention runs straight on into lower-case prose ("Table 2
    compares ...", "Figure 4 shows ...").
    """
    after = text[m.end(1):]
    if re.match(r"^\s*[.:|\u2013\u2014-]", after):
        return False                      # explicit separator: a caption
    rest = after.strip()
    if not rest:
        return False                      # bare "TABLE 1" marker line
    if rest[0].islower():
        return True
    # a long multi-sentence run is prose even when it starts capitalised
    return len(rest.split()) > 40 and rest.count(". ") >= 2


def _is_prose_line(cap: str) -> bool:
    """Text after "Table N" that reads as a sentence rather than a title."""
    c = (cap or "").strip()
    if not c:
        return False
    words = c.split()
    if len(words) > 40:
        return True
    # two or more sentences
    if len(re.findall(r"[a-z0-9)\]]\.\s+[A-Z]", c)) >= 1 and len(words) > 18:
        return True
    # "Table 3 and Table 4 ...", "Tables 2-3 ...", "Table 3, the ..."
    if re.match(r"^(and|or|to|,|&|\u2013|-)\s", c, re.I):
        return True
    # a finite verb early on marks a sentence: "The results show ..."
    return bool(re.search(r"\b(is|are|was|were|shows?|showed|presents?|"
                          r"indicates?|demonstrates?|reveals?|compares?|"
                          r"lists?|summari[sz]es?|illustrates?|depicts?|"
                          r"displays?|reports?|gives?|provides?)\b",
                          " ".join(words[:6]), re.I)) and len(words) > 8


def _keep_missing_figure(mk: dict, rest: list) -> bool:
    """Only a short, caption-like line is worth a missing-artwork placeholder."""
    cap = mk.get("caption", "")
    if mk.get("styled"):
        return True
    return 0 < len(cap.split()) <= 25 and not _is_prose_line(cap)


def _looks_like_plain_heading(text: str, rest: list, idx: int) -> bool:
    t = text.strip()
    words = t.split()
    if not (1 <= len(words) <= 12) or len(t) > 110:
        return False
    if t[-1] in ".,;:?!" or "\u27eaMATH" in t or "\u27e6" in t:
        return False
    if not t[0].isupper() or FIG_MARK.match(t) or TAB_MARK.match(t):
        return False
    if re.match(r"^(where|here|and|then|thus|with|for)\b", t, re.I):
        return False
    # next non-empty item must be real prose (or a float/equation opening it)
    for k in range(idx + 1, min(idx + 4, len(rest))):
        kd, nxt, pl = rest[k]
        if kd == "table" or pl.get("images"):
            return False
        display = any(d for _e, _t, d in (pl.get("maths") or []))
        if not nxt:
            if display:
                return False
            continue
        return len(nxt.split()) >= 8 and _heading_level(nxt) is None
    return False


def _is_allcaps_heading(t: str) -> bool:
    t = t.strip()
    if not (3 < len(t) < 100):
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 3:
        return False
    if not all(c.isupper() for c in letters):
        return False
    if FIG_MARK.match(t) or TAB_MARK.match(t):
        return False
    if t.endswith("."):            # a sentence in caps, not a heading
        return False
    if len(t.split()) > 14:        # too long to be a heading
        return False
    return True


def _strip_number(text: str) -> str:
    m = NUMBERED_HEADING.match(text.strip())
    return m.group(3).strip() if m else text.strip().rstrip(":")


def _split_references(lines: list[str]) -> list[Reference]:
    refs, buf = [], ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        starts_new = bool(re.match(r"^\s*(\[\d+\]|\d+[.)])\s+", ln)) or (
            buf and re.match(r"^[A-Z][a-zA-Z'’-]+,\s+[A-Z]", ln)
        )
        if starts_new and buf:
            refs.append(_parse_ref(buf))
            buf = ln
        else:
            buf = f"{buf} {ln}".strip()
    if buf:
        refs.append(_parse_ref(buf))
    return refs


def _parse_ref(raw: str) -> Reference:
    raw = re.sub(r"^\s*(\[\d+\]|\d+[.)])\s*", "", raw).strip()
    r = Reference(raw=raw)
    d = DOI.search(raw)
    if d:
        r.doi = d.group(0).rstrip(".,;")
    y = re.search(r"\b(19|20)\d{2}\b", raw)
    if y:
        r.year = y.group(0)
    m = re.search(r"[“\"]([^”\"]{10,300})[”\"]", raw)
    if m:
        r.title = m.group(1)
    r.authors = [a.strip() for a in raw.split(",")[:3] if a.strip()][:3]
    return r


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
def _iter_body(doc):
    """Yield ('para', Paragraph) and ('table', Table) in true document order."""
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxPara
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "para", DocxPara(child, doc)
        elif tag == "tbl":
            yield "table", DocxTable(child, doc)
        elif tag in ("oMathPara", "oMath"):
            # Word puts display equations at body level, not inside a w:p
            yield "math", child
        elif tag == "sdt":
            # content control: unwrap and walk what is inside it
            content = child.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sdtContent")
            if content is None:
                continue
            for sub in content.iterchildren():
                st = sub.tag.split("}")[-1]
                if st == "p":
                    yield "para", DocxPara(sub, doc)
                elif st == "tbl":
                    yield "table", DocxTable(sub, doc)
                elif st in ("oMathPara", "oMath"):
                    yield "math", sub


NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_V = "urn:schemas-microsoft-com:vml"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_WPG = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"


def _element_rids(el) -> list[str]:
    """Every image relationship id under this element, whatever it is wrapped in.

    Covers inline DrawingML, floating anchors, images inside text boxes,
    grouped shapes, legacy VML, and OLE/embedded object previews. Anything
    that misses here shows up to the user as a silently dropped figure.
    """
    rids: list[str] = []
    for blip in el.findall(".//{%s}blip" % NS_A):
        rid = blip.get("{%s}embed" % NS_R) or blip.get("{%s}link" % NS_R)
        if rid:
            rids.append(rid)
    for imagedata in el.findall(".//{%s}imagedata" % NS_V):
        rid = imagedata.get("{%s}id" % NS_R) or imagedata.get("{%s}href" % NS_R)
        if rid:
            rids.append(rid)
    # OLE objects (Visio/Equation/Excel) expose a raster preview
    for shape in el.findall(".//{%s}object" % NS_W):
        for imagedata in shape.findall(".//{%s}imagedata" % NS_V):
            rid = imagedata.get("{%s}id" % NS_R)
            if rid:
                rids.append(rid)
    seen, out = set(), []
    for r in rids:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _save_rids(rids, doc, image_dir: str) -> list[str]:
    out = []
    Path(image_dir).mkdir(parents=True, exist_ok=True)
    for rid in rids:
        try:
            part = doc.part.related_parts[rid]
        except KeyError:
            continue
        name = os.path.basename(str(getattr(part, "partname", f"{rid}.png")))
        dest = os.path.join(image_dir, f"{rid}_{name}")
        if not os.path.exists(dest):
            try:
                with open(dest, "wb") as f:
                    f.write(part.blob)
            except Exception:
                continue
        out.append(dest)
    return out


def _para_images(par, doc, image_dir: str) -> list[str]:
    return _save_rids(_element_rids(par._p), doc, image_dir)


def _table_images(tbl, doc, image_dir: str) -> list[str]:
    """Images living inside table cells — common for figure grids."""
    return _save_rids(_element_rids(tbl._tbl), doc, image_dir)


EQ_NUM_ONLY = re.compile(r"^[\s\u00a0]*[\(\[]?\s*(\d{1,3}[a-z]?)\s*[\)\]]?[\s\u00a0]*$")


def _residual_text(par) -> str:
    """Paragraph text with all equation content removed."""
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    MM = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    parts = []
    for node in par._p.iter():
        if node.tag != W + "t":
            continue
        if any(a.tag in (MM + "oMath", MM + "oMathPara") for a in node.iterancestors()):
            continue
        parts.append(node.text or "")
    return "".join(parts)


def _classify_math(par, maths):
    """Decide which equations are display rather than inline, and find the number.

    Word writes a numbered display equation as a bare m:oMath sitting in the
    same paragraph as its number — there is no oMathPara to key off. Treating
    those as inline is what leaks a placeholder into the output. The reliable
    signal is the paragraph's residual text: if removing the maths leaves
    nothing, or leaves only something like "(11)", the equation is display.
    """
    if not maths:
        return maths, None
    residual = _residual_text(par).strip()
    m = EQ_NUM_ONLY.match(residual)
    if residual == "" or m:
        number = m.group(1) if m else None
        return [(el, tex, True) for el, tex, _ in maths], number
    return maths, None


def _para_text_with_math(par, maths) -> str:
    """Paragraph text with inline equations restored in the right place.

    python-docx's .text walks w:r runs only and skips m:oMath entirely, which is
    why sentences come out as "where  is the real power flow". Walk the XML in
    document order instead and splice the LaTeX back in.
    """
    if not maths:
        return par.text
    display_only = all(is_disp for _, _, is_disp in maths)
    if display_only:
        return _residual_text(par)
    inline = {id(el): tex for el, tex, is_disp in maths if not is_disp}
    order = [el for el, _, is_disp in maths if not is_disp]
    slot = {id(el): i for i, el in enumerate(order)}
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    MM = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    parts, seen = [], set()
    for node in par._p.iter():
        if node.tag == MM + "oMath":
            if id(node) in inline and id(node) not in seen:
                seen.add(id(node))
                # placeholder: each renderer substitutes its own math syntax
                parts.append(f"\u27eaMATH{slot[id(node)]}\u27eb")
        elif node.tag == W + "t":
            # skip text that lives inside an equation — already handled above
            if any(a.tag == MM + "oMath" for a in node.iterancestors()):
                continue
            parts.append(node.text or "")
    return "".join(parts)


def _normalise_heading_levels(ms) -> None:
    """Decide whether unnumbered ALL-CAPS headings are sections or subsections.

    They are assumed to be subsections while parsing, because that is the safe
    default when a document also numbers its top-level sections. But many client
    manuscripts number only a couple of headings ("I. Introduction",
    "V. Conclusion") and write every major topic in caps. Left as subsections,
    those become "A.", "B.", "C." under whichever numbered heading came first —
    the whole body collapses under one section and the paper reads as flat and
    disordered, especially in a two-column class.

    So: if the caps headings outnumber the numbered ones, they were the real
    sections and get promoted.
    """
    caps = [s for s in ms.sections if s.level == 2 and _is_allcaps_heading(s.title)]
    numbered = [s for s in ms.sections if s.level == 1]
    if not caps:
        return
    if len(caps) > len(numbered):
        for s in caps:
            s.level = 1


def _extract_comments(path: str) -> list[dict]:
    """Reviewer/supervisor comments stored in the .docx.

    Word keeps them in word/comments.xml, anchored to a text range in the body.
    They are invisible to python-docx, so they are simply lost on conversion —
    which matters, because they are usually the actual to-do list for the paper.
    """
    import zipfile as _zf
    from lxml import etree

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: list[dict] = []
    try:
        z = _zf.ZipFile(path)
    except Exception:
        return out
    if "word/comments.xml" not in z.namelist():
        return out
    try:
        root = etree.fromstring(z.read("word/comments.xml"))
    except Exception:
        return out

    for c in root.findall(W + "comment"):
        text = " ".join("".join(t.text or "" for t in p.findall(".//" + W + "t"))
                        for p in c.findall(W + "p")).strip()
        if not text:
            continue
        out.append({
            "id": c.get(W + "id", ""),
            "author": c.get(W + "author", "") or "unknown",
            "date": (c.get(W + "date", "") or "")[:10],
            "text": _clean(text),
            "anchor": "",
        })

    # find the text each comment is attached to
    try:
        doc_root = etree.fromstring(z.read("word/document.xml"))
    except Exception:
        return out
    by_id = {c["id"]: c for c in out}
    for start in doc_root.findall(".//" + W + "commentRangeStart"):
        cid = start.get(W + "id")
        if cid not in by_id:
            continue
        words, node = [], start
        for _ in range(400):
            node = node.getnext() if node.getnext() is not None else None
            if node is None:
                break
            if node.tag == W + "commentRangeEnd" and node.get(W + "id") == cid:
                break
            words.extend(t.text or "" for t in node.findall(".//" + W + "t"))
        anchor = _clean(" ".join(words))
        by_id[cid]["anchor"] = anchor[:160]
    return out


def _cell_text(cell) -> str:
    """Cell text with inline equations kept as ⟦latex⟧ tokens.

    ``cell.text`` skips m:oMath, so symbol-only cells (a column headed "n", a
    row "c = 0, α = 1") came out empty and headers like "RMSE(α)" as "RMSE()".
    """
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    MM = omml.M
    if cell._tc.find(".//" + MM + "oMath") is None:
        return cell.text
    paras = []
    for p in cell._tc.iter(W + "p"):
        parts = []
        for node in p.iter():
            if node.tag == MM + "oMath":
                if not any(a.tag == MM + "oMath" for a in node.iterancestors()):
                    parts.append(" " + inline_math.wrap(omml.omml_to_latex(node)) + " ")
            elif node.tag == W + "t":
                if any(a.tag == MM + "oMath" for a in node.iterancestors()):
                    continue
                parts.append(node.text or "")
        paras.append("".join(parts))
    txt = "\n".join(paras)
    # tidy the padding added around tokens without touching the maths
    txt = re.sub(r" +(\u27e7)", r"\1", re.sub(r"(\u27e6) +", r"\1", txt))
    txt = re.sub(r"\( +\u27e6", "(\u27e6", txt)
    txt = re.sub(r"\u27e7 +\)", "\u27e7)", txt)
    return txt


def _table_caption_attr(tbl) -> str:
    """Caption stored on the table itself (w:tblCaption — written by pandoc,
    LibreOffice and Word's Alt Text). A reliable fallback when the visible
    caption paragraph is unlabelled."""
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    el = tbl._tbl.find(W + "tblPr/" + W + "tblCaption")
    return _clean(el.get(W + "val", "")) if el is not None else ""


def _table_rows(tbl) -> list[list[str]]:
    """Rows as text, preserving grid width.

    python-docx repeats the same tc element across a horizontal merge. Dropping
    the repeats (what we did before) shortens the row and shifts every later
    cell left, silently corrupting the table. Keep the grid width and blank the
    continuation cells instead.
    """
    rows = []
    for r in tbl.rows:
        cells, seen = [], set()
        for c in r.cells:
            key = id(c._tc)
            if key in seen:
                cells.append("")          # merge continuation — hold the slot
            else:
                seen.add(key)
                nested = [_clean(" | ".join(
                    " ".join(x.text.split()) for x in nt.rows[0].cells))
                    for nt in c.tables] if c.tables else []
                txt = _clean(_cell_text(c))
                if nested and not txt:
                    txt = "; ".join(n for n in nested if n)
                cells.append(txt)
        rows.append(cells)
    # drop wholly empty trailing columns produced by stray grid entries
    while rows and all(len(r) > 1 and not r[-1] for r in rows):
        for r in rows:
            r.pop()
    return rows


def from_docx(path: str, image_dir: str | None = None) -> Manuscript:
    from docx import Document

    doc = Document(path)
    ms = Manuscript()
    image_dir = image_dir or tempfile.mkdtemp(prefix="jf_img_")

    # ---- flatten the body, keeping order, images and tables in place -------
    items = []   # (kind, text, payload)
    for kind, obj in _iter_body(doc):
        if kind == "para":
            imgs = _para_images(obj, doc, image_dir)
            maths = omml.paragraph_math(obj._p)
            maths, eq_number = _classify_math(obj, maths)
            text = _clean(_para_text_with_math(obj, maths))
            if eq_number is not None:
                # the number belongs to the equation, not the body text
                text = ""
            from lxml import etree as _et
            inline_eqs = []
            for el, tex, is_disp in maths:
                if is_disp:
                    continue
                try:
                    xml = _et.tostring(el, encoding="unicode")
                except Exception:
                    xml = ""
                inline_eqs.append({"latex": tex, "omml": xml})
            style = (obj.style.name or "") if obj.style is not None else ""
            items.append(("para", text, {"images": imgs, "style": style,
                                         "maths": maths, "eq_number": eq_number,
                                         "inline_eqs": inline_eqs}))
        elif kind == "math":
            maths = [(el, omml.omml_to_latex(el), True)
                     for el in ([obj] if obj.tag.endswith("}oMath")
                                else obj.findall(omml.M + "oMath"))]
            items.append(("para", "", {"images": [], "style": "",
                                       "maths": maths}))
        else:
            items.append(("table", "", {"rows": _table_rows(obj),
                                        "caption_attr": _table_caption_attr(obj),
                                        "images": _table_images(obj, doc, image_dir)}))

    def _is_caption_style(style: str) -> bool:
        return "caption" in (style or "").lower()

    # ---- front matter -----------------------------------------------------
    i = 0
    while i < len(items) and not items[i][1]:
        i += 1
    if i < len(items):
        ms.title = _clean(items[i][1])
        i += 1

    author_block = []
    j = i
    while j < len(items) and not re.match(r"^\s*abstract", items[j][1], re.I) and j - i < 40:
        if items[j][1]:
            author_block.append(items[j][1])
        j += 1
    i = j
    for line in author_block[:6]:
        if EMAIL.search(line) or len(line.split(",")) <= 6:
            for nm in re.split(r",| and ", line):
                nm = nm.strip()
                if 2 <= len(nm.split()) <= 5 and not EMAIL.search(nm) and nm[:1].isupper():
                    ms.authors.append(Author(name=nm))
            break
    emails = EMAIL.findall(" ".join(author_block))
    for a, e in zip(ms.authors, emails):
        a.email = e
    aff = [l for l in author_block if re.search(
        r"universit|institute|college|department|school|dept", l, re.I)]
    if aff:
        for a in ms.authors:
            a.affiliation = a.affiliation or aff[0]

    # ---- abstract + keywords ---------------------------------------------
    if i < len(items):
        abs_txt = re.sub(r"^\s*abstract\s*[-\u2014:.]*\s*", "", items[i][1], flags=re.I)
        i += 1
        while i < len(items) and items[i][0] == "para" \
                and not re.match(r"^\s*(index terms|keywords)", items[i][1], re.I) \
                and _heading_level(items[i][1]) is None:
            if items[i][1]:
                abs_txt += " " + items[i][1]
            i += 1
        ms.abstract = abs_txt.strip()
    if i < len(items) and re.match(r"^\s*(index terms|keywords)", items[i][1], re.I):
        kw = re.sub(r"^\s*(index terms|keywords)\s*[-\u2014:.]*\s*", "", items[i][1], flags=re.I)
        ms.keywords = [k.strip(" .;") for k in re.split(r"[,;]", kw) if k.strip()]
        i += 1

    # ---- body -------------------------------------------------------------
    current = Section(title="")
    in_refs = False
    ref_lines: list[str] = []
    rest = items[i:]

    def _nearby_caption(idx: int, pattern) -> tuple[str, int | None]:
        """Look one or two items back, then forward, for a caption line."""
        for k in (idx - 1, idx + 1, idx - 2, idx + 2):
            if 0 <= k < len(rest) and rest[k][0] == "para":
                m = pattern.match(rest[k][1])
                if m:
                    return m.group(3).strip(), k
                if _is_caption_style(rest[k][2].get("style", "")) and rest[k][1]:
                    return rest[k][1], k
        return "", None

    # ------------------------------------------------------------------ #
    # Pre-pass 1: find caption markers.
    #
    # Client docs use several conventions, sometimes in the same file:
    #     "Fig. 1. Drawbacks of IDS"     -> marker + caption on one line
    #     "TABLE 1" / "The area, no of.."-> marker line, caption on the next
    #     "Fig. 3."                      -> marker with no caption at all
    # The author's own numbering is preserved so in-text cross-references
    # ("as shown in Table 14") stay valid.
    # ------------------------------------------------------------------ #
    markers: list[dict] = []          # {kind, num, caption, idx}
    consumed: set[int] = set()

    def _obj_kind(k: int) -> str | None:
        if not (0 <= k < len(rest)):
            return None
        kd, _t, pl = rest[k]
        if kd == "table":
            has_text = any(c.strip() for r in pl["rows"] for c in r)
            return "figure" if (pl.get("images") and not has_text) else "table"
        if pl.get("images"):
            return "figure"
        return None

    for idx, (kind, text, payload) in enumerate(rest):
        if kind != "para" or not text or idx in consumed:
            continue
        cap_text = _clean(inline_math.from_placeholders(
            text, payload.get("inline_eqs") or []))
        fm, tm = FIG_MARK.match(cap_text), TAB_MARK.match(cap_text)
        m, mk = (fm, "figure") if fm else ((tm, "table") if tm else (None, None))
        styled = _is_caption_style(payload.get("style", ""))
        if m and _is_body_mention(cap_text, m) and not styled:
            # "Figure 2 displays RMSE ..." is a sentence *about* the figure.
            # Taking it as the caption put the discussion paragraph into the
            # caption slot, stripped "Figure 2" from the text, and bound the
            # marker to the wrong image — shifting every later figure by one.
            m = None
        if not m:
            if styled and len(cap_text.split()) <= 60:
                # unlabelled caption paragraph (pandoc/Word "Table Caption",
                # "Image Caption"): its kind comes from the object beside it
                near = [_obj_kind(k) for k in (idx + 1, idx - 1, idx + 2, idx - 2)]
                near = [n for n in near if n]
                if near:
                    consumed.add(idx)
                    markers.append({"kind": near[0], "num": None,
                                    "caption": cap_text, "idx": idx})
            continue
        num = _numval(m.group(1))
        if num is None:
            continue
        cap = _clean(m.group(2))
        if not styled and _is_prose_line(cap):
            # "Table 3. The results in this table show ..." — a sentence that
            # merely begins with a table/figure mention, not a caption
            continue
        consumed.add(idx)
        extra = None
        # caption on the following line?
        if not cap:
            for k in range(idx + 1, min(idx + 3, len(rest))):
                if rest[k][0] != "para":
                    break
                nxt = _clean(inline_math.from_placeholders(
                    rest[k][1], rest[k][2].get("inline_eqs") or []))
                if not nxt:
                    continue
                if FIG_MARK.match(nxt) or TAB_MARK.match(nxt) or _is_allcaps_heading(nxt):
                    break
                if len(nxt.split()) <= 25 and not nxt.endswith(("!", "?")):
                    cap = nxt
                    consumed.add(k)
                    extra = k
                break
        markers.append({"kind": mk, "num": num, "caption": cap, "idx": idx,
                        "extra": extra, "styled": styled})

    def _take_marker(idx: int, want: str) -> dict | None:
        """Nearest unused marker of the right kind, preferring the one above."""
        best, best_d = None, 99
        for mk in markers:
            if mk["kind"] != want or mk.get("used"):
                continue
            d = abs(mk["idx"] - idx)
            # a marker above the object is the stronger signal for tables,
            # below for figures, but both are accepted within a short window
            if d > 4:
                continue
            if want == "figure" and mk["idx"] > idx:
                d -= 0.5
            if want == "table" and mk["idx"] < idx:
                d -= 0.5
            # a marker whose number is the next one in sequence is the right
            # one; a stray mention of another number loses the tie
            expected = sum(1 for m2 in markers if m2["kind"] == want and m2.get("used")) + 1
            if mk.get("num") is not None and mk["num"] != expected:
                d += 1.5
            if d < best_d:
                best, best_d = mk, d
        if best is not None:
            best["used"] = True
        return best

    used_labels: set[str] = set()

    def _label(prefix: str, mk: dict | None, fallback: int) -> str:
        n = (mk.get("num") if mk else None) or fallback
        lbl = f"{prefix}{n}"
        while lbl in used_labels:
            n += 1
            lbl = f"{prefix}{n}"
        used_labels.add(lbl)
        return lbl

    # ------------------------------------------------------------------ #
    # Pre-pass 2: bind markers to the real objects (tables and images).
    # ------------------------------------------------------------------ #
    bound: dict[int, dict] = {}
    for idx, (kind, text, payload) in enumerate(rest):
        if kind == "table":
            imgs = payload.get("images") or []
            has_text = any(c.strip() for r in payload["rows"] for c in r)
            want = "figure" if (imgs and not has_text) else "table"
            bound[idx] = _take_marker(idx, want) or {}
        elif payload.get("images"):
            bound[idx] = _take_marker(idx, "figure") or {}

    # ------------------------------------------------------------------ #
    # Main walk.
    # ------------------------------------------------------------------ #
    for idx, (kind, text, payload) in enumerate(rest):

        if kind == "table":
            if in_refs:
                continue
            imgs = payload.get("images") or []
            has_text = any(c.strip() for r in payload["rows"] for c in r)
            if imgs and not has_text:
                # a "table" used purely as a layout frame for figures
                mk = bound.get(idx) or {}
                for n_img, img in enumerate(imgs):
                    label = _label("fig", mk if n_img == 0 else None,
                                   len(ms.figures) + 1)
                    ms.figures.append(Figure(
                        label=label,
                        caption=mk.get("caption", "") if n_img == 0 else "",
                        path=img))
                    current.blocks.append(Block(kind="figure", ref=label))
                continue
            mk = bound.get(idx) or {}
            label = _label("tab", mk or None, len(ms.tables) + 1)
            ms.tables.append(Table(label=label,
                                   caption=mk.get("caption", "")
                                   or payload.get("caption_attr", ""),
                                   rows=payload["rows"]))
            current.blocks.append(Block(kind="table", ref=label))
            for img in imgs:
                lbl = _label("fig", None, len(ms.figures) + 1)
                ms.figures.append(Figure(label=lbl, caption="", path=img))
                current.blocks.append(Block(kind="figure", ref=lbl))
            continue

        # ---------------- display equations --------------------------------
        disp = [(el, tex) for el, tex, is_disp in (payload.get("maths") or [])
                if is_disp]
        if disp and not in_refs:
            from lxml import etree as _et
            for el, tex in disp:
                if not tex.strip():
                    continue
                label = f"eq{len(ms.equations) + 1}"
                try:
                    xml = _et.tostring(el, encoding="unicode")
                except Exception:
                    xml = ""
                num = payload.get("eq_number")
                tex, typed_num = _split_typed_number(tex)
                num = num or typed_num
                ms.equations.append(Equation(
                    label=label, latex=tex, omml=xml,
                    number=num if num else len(ms.equations) + 1))
                current.blocks.append(Block(kind="equation", ref=label))
            if not text:
                continue

        if payload.get("images") and not in_refs:
            mk = bound.get(idx) or {}
            for n_img, img in enumerate(payload["images"]):
                label = _label("fig", mk or None, len(ms.figures) + 1)
                ms.figures.append(Figure(label=label,
                                         caption=mk.get("caption", "") if n_img == 0 else "",
                                         path=img))
                current.blocks.append(Block(kind="figure", ref=label))
            if not text or idx in consumed:
                continue

        if idx in consumed:
            # a caption marker whose object never materialised (missing image):
            # keep it as a placeholder so the author can see what is absent
            mk = next((m for m in markers if m["idx"] == idx and not m.get("used")), None)
            owner = next((m for m in markers if m.get("extra") == idx), None)
            if owner is not None and owner.get("orphan") and not in_refs:
                # follow-on "caption" line of a marker that bound to nothing:
                # it was ordinary text all along
                if text:
                    blk = Block(kind="para", text=text)
                    blk.inline_eqs = payload.get("inline_eqs") or []
                    current.blocks.append(blk)
                continue
            if mk and not in_refs:
                mk["used"] = True
                mk["orphan"] = not (mk["kind"] == "figure" and _keep_missing_figure(mk, rest))
                if not mk["orphan"]:
                    # an isolated "Fig. 3. Caption" line whose image is absent:
                    # keep a placeholder so the author sees what is missing
                    label = _label("fig", mk, len(ms.figures) + 1)
                    ms.figures.append(Figure(label=label, caption=mk["caption"], path=""))
                    current.blocks.append(Block(kind="figure", ref=label))
                else:
                    # A table marker with no table beside it is never a table:
                    # creating one pushed every later table number up by one
                    # ("Table 3" in the text became a phantom table 3). Put the
                    # line back as text. Its consumed caption line was already
                    # restored by the branch above, or is restored here.
                    if text:
                        blk = Block(kind="para", text=text)
                        blk.inline_eqs = payload.get("inline_eqs") or []
                        current.blocks.append(blk)
            continue

        if not text:
            continue

        lvl = _heading_level(text)
        style_lvl = _style_heading_level(payload.get("style", ""))
        if style_lvl and not lvl:
            lvl = style_lvl
        if not lvl and _looks_like_plain_heading(text, rest, idx):
            # short unpunctuated line in body style ("Simulation design")
            # followed by prose: a subsection the author never styled
            lvl = 2 if any(sec.level == 1 for sec in ms.sections) or current.title else 1
        clean = _strip_number(text)

        if lvl and clean.lower().startswith("reference"):
            if current.blocks or current.title:
                ms.sections.append(current)
            current = Section(title="")
            in_refs = True
            continue
        if in_refs:
            ref_lines.append(text)
            continue

        if lvl:
            if current.blocks or current.title:
                ms.sections.append(current)
            current = Section(title=clean, level=lvl)
            continue

        blk = Block(kind="para", text=text)
        blk.inline_eqs = payload.get("inline_eqs") or []
        current.blocks.append(blk)

    if current.blocks or current.title:
        ms.sections.append(current)
    _normalise_heading_levels(ms)
    ms.references = _split_references(ref_lines)
    ms.comments = _extract_comments(path)
    return ms


# --------------------------------------------------------------------------- #
# LaTeX
# --------------------------------------------------------------------------- #
def from_latex(tex: str) -> Manuscript:
    ms = Manuscript()
    tex = re.sub(r"(?<!\\)%.*", "", tex)

    m = re.search(r"\\title\s*\{(.+?)\}\s*(?:\n|\\)", tex, re.S)
    if m:
        ms.title = re.sub(r"\s+", " ", m.group(1)).strip()
    for am in re.finditer(r"\\author\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}", tex):
        for nm in re.split(r",| and ", am.group(1)):
            nm = nm.strip()
            if nm:
                ms.authors.append(Author(name=nm))
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    if m:
        ms.abstract = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"\\(?:keywords|IEEEkeywords|keyword)\s*\{?(.*?)(?:\}|\\end\{)", tex, re.S)
    if m:
        ms.keywords = [k.strip() for k in re.split(r"[,;]", m.group(1)) if k.strip()]

    body = tex
    m = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", tex, re.S)
    if m:
        body = m.group(1)

    for em in re.finditer(r"\\begin\{(equation|align)\*?\}(.*?)\\end\{\1\*?\}", body, re.S):
        lbl = re.search(r"\\label\{([^}]+)\}", em.group(2))
        ms.equations.append(Equation_safe(lbl.group(1) if lbl else f"eq{len(ms.equations)+1}",
                                          em.group(2).strip()))
    for fm in re.finditer(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", body, re.S):
        cap = re.search(r"\\caption\{(.+?)\}\s*(?:\\label|\n|$)", fm.group(1), re.S)
        lbl = re.search(r"\\label\{([^}]+)\}", fm.group(1))
        src = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", fm.group(1))
        ms.figures.append(Figure(label=lbl.group(1) if lbl else f"fig{len(ms.figures)+1}",
                                 caption=(cap.group(1).strip() if cap else ""),
                                 path=src.group(1) if src else ""))

    parts = re.split(r"\\(section|subsection|subsubsection)\*?\{(.+?)\}", body)
    if len(parts) > 1:
        it = iter(parts[1:])
        for kind, title, content in zip(it, it, it):
            lvl = {"section": 1, "subsection": 2, "subsubsection": 3}[kind]
            sec = Section(title=title.strip(), level=lvl)
            for para in re.split(r"\n\s*\n", content):
                para = re.sub(r"\\begin\{(figure|table|equation|align)\*?\}.*?"
                              r"\\end\{\1\*?\}", "", para, flags=re.S)
                para = re.sub(r"\s+", " ", para).strip()
                if para and not para.startswith("\\"):
                    sec.blocks.append(Block(kind="para", text=para))
            ms.sections.append(sec)

    for bm in re.finditer(r"\\bibitem(?:\[[^\]]*\])?\{[^}]*\}(.+?)(?=\\bibitem|\\end\{thebibliography\})",
                          body, re.S):
        ms.references.append(_parse_ref(re.sub(r"\s+", " ", bm.group(1)).strip()))
    return ms


def Equation_safe(label, latex):
    from .model import Equation
    return Equation(label=label, latex=latex)


# --------------------------------------------------------------------------- #
# PDF (lossy — text only, use as a last resort)
# --------------------------------------------------------------------------- #
def from_pdf(path: str) -> Manuscript:
    import pdfplumber
    lines: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines.extend(txt.split("\n"))
    # reflow: join lines that don't end a sentence
    paras, buf = [], ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            if buf:
                paras.append(buf); buf = ""
            continue
        if _heading_level(ln):
            if buf:
                paras.append(buf); buf = ""
            paras.append(ln)
            continue
        buf = f"{buf} {ln}".strip() if buf else ln
        if ln.endswith((".", "?", "!")) and len(buf) > 120:
            paras.append(buf); buf = ""
    if buf:
        paras.append(buf)
    return _from_paragraphs(paras)


def _from_paragraphs(paras: list[str]) -> Manuscript:
    ms = Manuscript()
    if paras:
        ms.title = paras[0]
    current = Section(title="Body")
    in_refs = False
    ref_lines = []
    for text in paras[1:]:
        lvl = _heading_level(text)
        clean = _strip_number(text)
        if re.match(r"^\s*abstract", text, re.I) and not ms.abstract:
            ms.abstract = re.sub(r"^\s*abstract\s*[-—:.]*\s*", "", text, flags=re.I)
            continue
        if re.match(r"^\s*(keywords|index terms)", text, re.I) and not ms.keywords:
            kw = re.sub(r"^\s*(keywords|index terms)\s*[-—:.]*\s*", "", text, flags=re.I)
            ms.keywords = [k.strip(" .;") for k in re.split(r"[,;]", kw) if k.strip()]
            continue
        if lvl and clean.lower().startswith("reference"):
            if current.blocks:
                ms.sections.append(current)
            current = Section(title="")
            in_refs = True
            continue
        if in_refs:
            ref_lines.append(text); continue
        if lvl:
            if current.blocks:
                ms.sections.append(current)
            current = Section(title=clean, level=lvl)
        else:
            blk = Block(kind="para", text=text)
        blk.inline_eqs = payload.get("inline_eqs") or []
        current.blocks.append(blk)
    if current.blocks:
        ms.sections.append(current)
    ms.references = _split_references(ref_lines)
    return ms


# --------------------------------------------------------------------------- #
def ingest(path: str, image_dir: str | None = None) -> Manuscript:
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return from_docx(path, image_dir)
    if ext in (".tex", ".txt"):
        return from_latex(Path(path).read_text(errors="ignore"))
    if ext == ".pdf":
        return from_pdf(path)
    if ext == ".zip":
        d = tempfile.mkdtemp(prefix="jf_zip_")
        with zipfile.ZipFile(path) as z:
            z.extractall(d)
        texs = list(Path(d).rglob("*.tex"))
        if not texs:
            raise ValueError("No .tex file found in the archive.")
        main = max(texs, key=lambda p: len(p.read_text(errors="ignore")))
        return from_latex(main.read_text(errors="ignore"))
    raise ValueError(f"Unsupported file type: {ext}")
