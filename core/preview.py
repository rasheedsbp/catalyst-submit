"""Preview a formatted manuscript.

Two paths:
  1. render_pages()  — LibreOffice → PDF → PNG page images. True WYSIWYG, needs
     `soffice` and `pdftoppm` on PATH. Available locally; usually NOT on
     Streamlit Community Cloud.
  2. html_preview()  — pure-Python HTML approximation built from the profile's
     Word spec. Always works. Good enough to catch layout mistakes before
     downloading.
"""
from __future__ import annotations

from . import inline_math as _im
import html
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .model import Manuscript

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
         "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]


def soffice_available() -> bool:
    return bool(shutil.which("soffice") or shutil.which("libreoffice")) and \
        bool(shutil.which("pdftoppm"))


def render_pages(docx_bytes: bytes, dpi: int = 110, max_pages: int = 12) -> list[bytes]:
    """Convert a .docx to a list of PNG page images. Returns [] if tools missing."""
    if not soffice_available():
        return []
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    d = tempfile.mkdtemp(prefix="jf_prev_")
    src = os.path.join(d, "doc.docx")
    with open(src, "wb") as f:
        f.write(docx_bytes)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", d, src],
            check=True, capture_output=True, timeout=180,
        )
        pdf = os.path.join(d, "doc.pdf")
        if not os.path.exists(pdf):
            return []
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-l", str(max_pages),
             pdf, os.path.join(d, "pg")],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [Path(p).read_bytes() for p in sorted(Path(d).glob("pg-*.png"))]


def render_pdf(docx_bytes: bytes) -> bytes | None:
    """Convert .docx to PDF bytes for download / inline embed."""
    if not soffice_available():
        return None
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    d = tempfile.mkdtemp(prefix="jf_pdf_")
    src = os.path.join(d, "doc.docx")
    with open(src, "wb") as f:
        f.write(docx_bytes)
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                        "--outdir", d, src],
                       check=True, capture_output=True, timeout=180)
    except Exception:
        return None
    pdf = Path(d) / "doc.pdf"
    return pdf.read_bytes() if pdf.exists() else None


# --------------------------------------------------------------------------- #
# HTML fallback
# --------------------------------------------------------------------------- #
def _case(s: str, mode: str) -> str:
    small = {"a", "an", "the", "and", "or", "of", "in", "on", "for", "to",
             "with", "at", "by", "from"}
    if mode == "upper":
        return s.upper()
    if mode == "title":
        return " ".join(w.capitalize() if (i == 0 or w.lower() not in small) else w.lower()
                        for i, w in enumerate(s.split()))
    if mode == "sentence":
        return s[:1].upper() + s[1:] if s else s
    return s


def html_preview(ms: Manuscript, prof: dict) -> str:
    w = prof["word"]
    e = html.escape
    page_w = "8.27in" if w["page_size"] == "a4" else "8.5in"
    m = w["margins_in"]
    cols = w["columns"]

    css = f"""
    <meta charset="utf-8">
    <style>
      .jf-page {{
        width: {page_w}; max-width: 100%; background: #fff; color: #111;
        margin: 0 auto; padding: {m['top']}in {m['right']}in {m['bottom']}in {m['left']}in;
        box-shadow: 0 0 12px rgba(0,0,0,.18); box-sizing: border-box;
        font-family: "{w['body_font']}", "Times New Roman", serif;
        font-size: {w['body_size']}pt; line-height: {w['line_spacing']};
        text-align: justify;
      }}
      .jf-title {{ font-size: {w['title_size']}pt; font-weight: 700;
                   text-align: center; line-height: 1.15; margin: 0 0 10px; }}
      .jf-authors {{ text-align: center; font-size: {w['body_size']+1}pt; margin: 2px 0; }}
      .jf-aff {{ text-align: center; font-style: italic;
                 font-size: {w['body_size']-1}pt; margin: 2px 0; }}
      .jf-cols {{ column-count: {cols}; column-gap: 0.25in; }}
      .jf-keep {{ break-inside: avoid; page-break-inside: avoid; }}
      .jf-h1, .jf-h2 {{ break-after: avoid; }}
      .jf-h1 {{ font-weight: 700; font-size: {w['body_size']+1}pt;
                margin: 12px 0 4px; text-align: {'center' if prof['publisher']=='IEEE' else 'left'}; }}
      .jf-h2 {{ font-weight: 700; font-style: italic; margin: 8px 0 3px; }}
      .jf-p {{ margin: 0 0 6px; text-indent: 0.2in; }}
      .jf-cap {{ text-align: center; font-size: {w['body_size']-1}pt; margin: 4px 0 10px; }}
      .jf-tbl {{ border-collapse: collapse; width: 100%; margin: 0 auto 10px;
                 font-size: {w['body_size']-2}pt; table-layout: fixed;
                 word-wrap: break-word; }}
      .jf-wide {{ column-span: all; font-size: {w['body_size']-3}pt; }}
      .jf-wide table {{ font-size: {w['body_size']-3}pt; }}
      .jf-tbl td, .jf-tbl th {{ border: 1px solid #333; padding: 2px 4px; }}
      .jf-ref {{ font-size: {w['body_size']-1}pt; margin: 0 0 3px; text-indent: 0; }}
      .jf-fig img {{ display:block; margin: 0 auto; max-width: 100%; }}
    </style>
    """

    head = [f'<div class="jf-title">{e(ms.title)}</div>']
    if ms.authors:
        head.append(f'<div class="jf-authors">{e(", ".join(a.name for a in ms.authors))}</div>')
        aff = next((a.affiliation for a in ms.authors if a.affiliation), "")
        if aff:
            head.append(f'<div class="jf-aff">{e(aff)}</div>')
        em = [a.email for a in ms.authors if a.email]
        if em:
            head.append(f'<div class="jf-aff">{e(", ".join(em))}</div>')

    body = []
    if prof.get("abstract", {}).get("type") == "structured" and ms.structured_abstract:
        for k, v in ms.structured_abstract.items():
            body.append(f'<p class="jf-p"><b>{e(k)} – </b>{e(v)}</p>')
    elif ms.abstract:
        lbl = "Abstract— " if prof["publisher"] == "IEEE" else "Abstract: "
        it = ' style="font-style:italic"' if prof["publisher"] == "IEEE" else ""
        body.append(f'<p class="jf-p"><b>{lbl}</b><span{it}>{e(ms.abstract)}</span></p>')
    if ms.keywords:
        lbl = "Index Terms— " if prof["publisher"] == "IEEE" else "Keywords: "
        kws = ", ".join(_case(k, prof.get("keywords", {}).get("case", "lower"))
                        for k in ms.keywords)
        body.append(f'<p class="jf-p"><b>{lbl}</b>{e(kws)}</p>')

    figs = {f.label: f for f in ms.figures}
    tabs = {t.label: t for t in ms.tables}
    eqs = {e.label: e for e in ms.equations}
    idx = 0
    for s in ms.sections:
        if s.level == 1:
            idx += 1
            n = f"{ROMAN[idx] if idx < len(ROMAN) else idx}. " \
                if w["heading_numbering"] == "roman_upper" else \
                (f"{idx}. " if w["heading_numbering"] == "decimal" else "")
            body.append(f'<div class="jf-h1">{e(n + _case(s.title, w["heading_case"]))}</div>')
        else:
            body.append(f'<div class="jf-h2">{e(_case(s.title, "sentence"))}</div>')
        for b in s.blocks:
            if b.kind == "para":
                txt = e(b.text)
                for i, ie in enumerate(getattr(b, "inline_eqs", None) or []):
                    txt = txt.replace(
                        f"\u27eaMATH{i}\u27eb",
                        f'<code style="font-style:italic">{e(ie.get("latex",""))}</code>')
                txt = re.sub("\u27eaMATH\\d+\u27eb", "", txt)
                body.append(f'<p class="jf-p">{txt}</p>')
            elif b.kind == "equation" and b.ref in eqs:
                eq = eqs[b.ref]
                num = f'<span style="float:right">({eq.number})</span>' if eq.number else ""
                body.append(
                    f'<div class="jf-keep" style="text-align:center;margin:6px 0">'
                    f'{num}<code style="font-size:{w["body_size"]-1}pt">'
                    f'{e(eq.latex)}</code></div>')
            elif b.kind == "figure" and b.ref in figs:
                f = figs[b.ref]
                num = "".join(c for c in f.label if c.isdigit()) or "1"
                if f.path and os.path.exists(f.path):
                    import base64
                    b64 = base64.b64encode(Path(f.path).read_bytes()).decode()
                    ext = Path(f.path).suffix.lstrip(".") or "png"
                    img = f'<div class="jf-fig"><img src="data:image/{ext};base64,{b64}"></div>'
                else:
                    img = ('<div class="jf-cap" style="border:1px dashed #999;'
                           'padding:18px">[figure image not extracted]</div>')
                body.append(f'<div class="jf-keep">{img}'
                            f'<div class="jf-cap">Fig. {num}.'
                            + (f' {e(_im.to_plain(f.caption))}' if f.caption else "")
                            + '</div></div>')
            elif b.kind == "table" and b.ref in tabs:
                t = tabs[b.ref]
                num = "".join(c for c in t.label if c.isdigit()) or "1"
                rows = []
                for ri, r in enumerate(t.rows):
                    tag = "th" if ri == 0 else "td"
                    rows.append("<tr>" + "".join(f"<{tag}>{e(_im.to_plain(c))}</{tag}>" for c in r) + "</tr>")
                tbl_html = f'<table class="jf-tbl">{"".join(rows)}</table>' if rows else ""
                ncol = max((len(r) for r in t.rows), default=0)
                wide = cols > 1 and ncol >= 4
                klass = "jf-keep jf-wide" if wide else "jf-keep"
                body.append(
                    f'<div class="{klass}"><div class="jf-cap"><b>TABLE {num}</b>'
                    + (f'<br>{e(_im.to_plain(t.caption))}' if t.caption else "")
                    + f'</div>{tbl_html}</div>')

    if ms.references:
        body.append(f'<div class="jf-h1">{e(_case("References", w["heading_case"]))}</div>')
        numeric = prof["reference_style"] in (
            "ieee", "elsevier-numeric", "multidisciplinary-digital-publishing-institute")
        for i, r in enumerate(ms.references, 1):
            txt = f"[{i}] {r.raw}" if numeric else r.raw
            style = "" if numeric else ' style="padding-left:.5in;text-indent:-.5in"'
            body.append(f'<p class="jf-ref"{style}>{e(txt)}</p>')

    return (css + '<div class="jf-page">' + "".join(head) +
            f'<div class="jf-cols">' + "".join(body) + "</div></div>")
