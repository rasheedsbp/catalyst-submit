"""Learn formatting *by example* from a journal's Word template.

Instead of guessing font sizes, every element we write (title, authors,
affiliation, headings, body, captions, references) is cloned from the matching
example paragraph in the publisher's template: its paragraph properties
(alignment, spacing, indents, list numbering) and its run properties (font,
size, bold/italic). Front-matter tables (Received/Accepted + Abstract box) are
kept and filled rather than deleted.
"""
from __future__ import annotations

import copy
import re

from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

W_P, W_TBL = qn("w:p"), qn("w:tbl")
FONT_ABBR = {"bos": "Bookman Old Style", "ba": "Book Antiqua",
             "tnr": "Times New Roman"}


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()


def _num_level(p: Paragraph):
    """Effective list level (direct or via style), or None."""
    for pPr in (p._p.pPr, getattr(p.style.element, "pPr", None) if p.style is not None else None):
        if pPr is None:
            continue
        n = pPr.find(qn("w:numPr"))
        if n is not None and n.find(qn("w:numId")) is not None \
                and n.find(qn("w:numId")).get(qn("w:val")) != "0":
            lv = n.find(qn("w:ilvl"))
            return int(lv.get(qn("w:val"))) if lv is not None else 0
    return None


def _snap(p: Paragraph, text_run_idx: int | None = None) -> dict:
    runs = [r for r in p.runs if r.text.strip()]
    r = runs[text_run_idx if text_run_idx is not None else 0] if runs else None
    pPr = copy.deepcopy(p._p.pPr) if p._p.pPr is not None else None
    if pPr is not None:
        for s in pPr.findall(qn("w:sectPr")):
            pPr.remove(s)
        rp = pPr.find(qn("w:rPr"))            # paragraph-mark props: harmless, drop
        if rp is not None:
            pPr.remove(rp)
    rPr = copy.deepcopy(r._r.rPr) if (r is not None and r._r.rPr is not None) else None
    t = p.text.strip()
    return {"pPr": pPr, "rPr": rPr, "numbered": _num_level(p) is not None,
            "upper": t.isupper() and len(t) > 3,
            "size": (r.font.size.pt if r is not None and r.font.size else None)}


def _parse_spec_table(tbl) -> dict:
    """Read a 'Component | ... | Font | ...' specification table if present."""
    out = {}
    rows = tbl.findall(qn("w:tr"))
    if not rows:
        return out
    head = [_text(c).lower() for c in rows[0].findall(qn("w:tc"))]
    if "component" not in " ".join(head) or not any("font" in h for h in head):
        return out
    fi = next(i for i, h in enumerate(head) if "font" in h)
    for tr in rows[1:]:
        cells = [_text(c) for c in tr.findall(qn("w:tc"))]
        if len(cells) <= fi:
            continue
        m = re.search(r"(\d+(?:\.\d)?)\s*pt", cells[fi], re.I)
        f = re.search(r"\b(BOS|BA|TNR)\b", cells[fi])
        out[cells[0].strip().lower()] = {
            "size": float(m.group(1)) if m else None,
            "font": FONT_ABBR.get(f.group(1).lower()) if f else None,
            "bold": "bold" in cells[fi].lower(),
            "italic": "italic" in cells[fi].lower()}
    return out


def collect(doc, children) -> dict:
    ex: dict = {}
    paras = [(i, Paragraph(c, doc)) for i, c in enumerate(children) if c.tag == W_P]
    nonempty = [(i, p) for i, p in paras if p.text.strip()]

    # title: largest bold run among the first non-empty paragraphs
    best = None
    for i, p in nonempty[:12]:
        runs = [r for r in p.runs if r.text.strip()]
        if runs and any(r.bold for r in runs) and len(p.text.strip()) > 10:
            sz = max((r.font.size.pt for r in runs if r.font.size), default=0)
            if best is None or sz > best[2]:
                best = (i, p, sz)
    if best:
        ex["title"] = _snap(best[1])
        after = [(i, p) for i, p in nonempty if i > best[0]][:6]
        for i, p in after:
            t = p.text.lower()
            if "authors" not in ex and any(r.bold for r in p.runs if r.text.strip()):
                ex["authors"] = _snap(p)
            elif "mail" in t and "email" not in ex:
                ex["email"] = _snap(p)
            elif "authors" in ex and "affil" not in ex and "mail" not in t:
                ex["affil"] = _snap(p)

    for i, p in nonempty:
        t = p.text.strip()
        sname = (p.style.name or "").lower() if p.style is not None else ""
        lvl = _num_level(p)
        bold = any(r.bold for r in p.runs if r.text.strip())
        short = len(t) < 80
        if "h1" not in ex and short and (sname == "heading 1" or (lvl == 0 and bold)):
            ex["h1"] = _snap(p)
        elif "h2" not in ex and short and (sname == "heading 2" or (lvl == 1 and bold)):
            ex["h2"] = _snap(p)
        elif "h3" not in ex and short and (sname == "heading 3" or lvl == 2):
            ex["h3"] = _snap(p)
        if "body" not in ex and len(t) > 200 and lvl is None and "list" not in sname:
            ex["body"] = _snap(p)
        if "figcap" not in ex and re.match(r"^fig(ure)?\.?\s*\d", t, re.I):
            ex["figcap"] = _snap(p)
        if "tabcap" not in ex and re.match(r"^table\s*\d", t, re.I):
            ex["tabcap"] = _snap(p)
        if "refhead" not in ex and re.fullmatch(r"references?", t, re.I):
            ex["refhead"] = _snap(p)
            nxt = [q for j, q in nonempty if j > i][:1]
            if nxt and len(nxt[0].text) > 30:
                ex["ref"] = _snap(nxt[0])

    for c in children:
        if c.tag != W_TBL:
            continue
        txt = _text(c)
        if "abs_tbl" not in ex and re.search(r"abstract", txt, re.I) and len(txt) < 3000:
            ex["abs_tbl"] = copy.deepcopy(c)
        spec = _parse_spec_table(c)
        if spec:
            ex["spec"] = spec
    return ex


def skin(p, role: dict, keep_run_style: bool = False) -> None:
    """Apply a template example's paragraph + run formatting to `p`."""
    if not role:
        return
    old = p._p.pPr
    if old is not None:
        p._p.remove(old)
    if role.get("pPr") is not None:
        p._p.insert(0, copy.deepcopy(role["pPr"]))
    for r in p.runs:
        if r._r.find(qn("w:drawing")) is not None or r._r.find(qn("w:object")) is not None:
            continue
        b, it = r.bold, r.italic
        if r._r.rPr is not None:
            r._r.remove(r._r.rPr)
        if role.get("rPr") is not None:
            r._r.insert(0, copy.deepcopy(role["rPr"]))
        if keep_run_style:
            if b is not None:
                r.bold = b
            if it is not None:
                r.italic = it


def fill_abstract_table(tbl, doc, abstract: str, keywords: str) -> None:
    """Replace the example abstract/keywords text but keep labels + formatting."""
    for pel in tbl.iter(W_P):
        p = Paragraph(pel, doc)
        txt = p.text.strip()
        m = re.match(r"^(abstract|keywords?|index terms)\s*[—\-–:]", txt, re.I)
        if not m:
            if txt and not re.match(r"^(received|revised|accepted|available)", txt, re.I):
                # stray example text (tabs, continuation lines) — clear it
                if not any(k in txt.lower() for k in ("received", "accepted")):
                    for r in list(p.runs):
                        pel.remove(r._r)
            elif not txt:
                for r in list(p.runs):
                    if r.text.strip() == "":
                        pel.remove(r._r)
            continue
        new = abstract if m.group(1).lower().startswith("abs") else keywords
        runs = list(p.runs)
        label_end, acc = 0, ""
        for k, r in enumerate(runs):
            acc += r.text
            if re.search(r"[—\-–:]\s*$", acc.strip() + " ") or re.search(r"[—\-–:]", r.text):
                label_end = k
                break
        body_fmt = None
        for r in runs[label_end + 1:]:
            if r.text.strip():
                body_fmt = copy.deepcopy(r._r.rPr) if r._r.rPr is not None else None
                break
        for r in runs[label_end + 1:]:
            pel.remove(r._r)
        nr = p.add_run(" " + new)
        if body_fmt is not None:
            nr._r.insert(0, body_fmt)


def keep_header_refs(body) -> None:
    """Carry first-section header/footer (the banner) into the final section.

    Templates with several sections keep the first-page header reference in the
    *first* section's sectPr, which lives inside a body paragraph that gets
    cleared. Without this the logo banner disappears.
    """
    final = body.find(qn("w:sectPr"))
    if final is None:
        return
    first = None
    for pPr in body.iter(qn("w:pPr")):
        s = pPr.find(qn("w:sectPr"))
        if s is not None:
            first = s
            break
    if first is None:
        return
    have = {(e.tag, e.get(qn("w:type"))) for e in final
            if e.tag in (qn("w:headerReference"), qn("w:footerReference"))}
    refs = [e for e in first if e.tag in (qn("w:headerReference"), qn("w:footerReference"))]
    for e in reversed(refs):
        if (e.tag, e.get(qn("w:type"))) not in have:
            final.insert(0, copy.deepcopy(e))
    if first.find(qn("w:titlePg")) is not None and final.find(qn("w:titlePg")) is None:
        tp = copy.deepcopy(first.find(qn("w:titlePg")))
        anchor = final.find(qn("w:docGrid"))
        (anchor.addprevious(tp) if anchor is not None else final.append(tp))
    # the banner section's page geometry is what the journal expects
    for tag in ("w:pgMar", "w:pgSz"):
        a, b = first.find(qn(tag)), final.find(qn(tag))
        if a is not None and b is not None:
            b.getparent().replace(b, copy.deepcopy(a))
