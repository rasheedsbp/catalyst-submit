"""Render a Manuscript into a LaTeX project matching a journal profile."""
from __future__ import annotations

import io
import os
import re
import re
import shutil
import zipfile
from pathlib import Path

from .model import Manuscript
from . import inline_math

# Characters that make pdflatex fail outright unless mapped.
UNICODE_TEX = {
    "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''",
    "\u2013": "--", "\u2014": "---", "\u2026": r"\ldots{}",
    "\u00a0": "~", "\u2009": r"\,", "\u202f": r"\,",
    "\u2212": "-", "\u00b7": r"\textperiodcentered{}",
    "\u2032": "$'$", "\u2033": "$''$", "\u00b0": r"$^{\circ}$",
    "\u00d7": r"$\times$", "\u00f7": r"$\div$", "\u00b1": r"$\pm$",
    "\u2264": r"$\leq$", "\u2265": r"$\geq$", "\u2260": r"$\neq$",
    "\u2248": r"$\approx$", "\u221e": r"$\infty$", "\u2211": r"$\sum$",
    "\u220f": r"$\prod$", "\u222b": r"$\int$", "\u221a": r"$\sqrt{}$",
    "\u2192": r"$\rightarrow$", "\u2190": r"$\leftarrow$",
    "\u21d2": r"$\Rightarrow$", "\u2208": r"$\in$",
    "\u00ad": "", "\ufeff": "", "\u200b": "", "\u200e": "", "\u200f": "",
    "\u2010": "-", "\u2011": "-", "\u2012": "--", "\u2015": "---",
    "\u02bc": "'", "\u00ab": "``", "\u00bb": "''", "\u2039": "`",
    "\u203a": "'", "\u2044": "/", "\u00a9": r"\copyright{}",
    "\u2122": r"\texttrademark{}", "\u00ae": r"\textregistered{}",
    "\u2020": r"\dag{}", "\u2021": r"\ddag{}", "\u00b5": r"$\mu$",
    "\u2032": "$'$",
    "\u27ea": "", "\u27eb": "",
}
GREEK_TEX = {
    "\u03b1": r"\alpha", "\u03b2": r"\beta", "\u03b3": r"\gamma",
    "\u03b4": r"\delta", "\u03b5": r"\epsilon", "\u03b8": r"\theta",
    "\u03bb": r"\lambda", "\u03bc": r"\mu", "\u03c0": r"\pi",
    "\u03c1": r"\rho", "\u03c3": r"\sigma", "\u03c4": r"\tau",
    "\u03c6": r"\phi", "\u03c9": r"\omega", "\u0394": r"\Delta",
    "\u03a3": r"\Sigma", "\u03a9": r"\Omega", "\u03a6": r"\Phi",
}


# accented Latin letters pdflatex can express with a standard accent command
_ACCENT_CMD = {
    "\u0300": "`", "\u0301": "'", "\u0302": "^", "\u0303": "~",
    "\u0304": "=", "\u0306": "u", "\u0307": ".", "\u0308": '"',
    "\u030a": "r", "\u030b": "H", "\u030c": "v", "\u0327": "c",
    "\u0328": "k", "\u0323": "d", "\u0331": "b",
}


# letters with no canonical decomposition — the fallback would lose them
_NO_DECOMP = {
    "\u00f8": r"\o{}", "\u00d8": r"\O{}", "\u0111": r"\dj{}",
    "\u0110": r"\DJ{}", "\u0142": r"\l{}", "\u0141": r"\L{}",
    "\u00e6": r"\ae{}", "\u00c6": r"\AE{}", "\u0153": r"\oe{}",
    "\u0152": r"\OE{}", "\u00df": "ss", "\u00fe": "th", "\u00de": "Th",
    "\u00f0": "d", "\u00d0": "D", "\u0131": r"\i{}", "\u0130": "I",
    "\u00e5": r"\aa{}", "\u00c5": r"\AA{}",
}


def _fallback_char(ch: str) -> str:
    """Express any remaining non-ASCII character in a way pdflatex accepts.

    Client reference lists are full of transliterated names — Muhammad, Tulbah,
    Zurich — carrying combining marks. pdflatex stops dead on each one with
    "Unicode character not set up for use with LaTeX", which is a compile
    failure the author sees on Overleaf and cannot easily diagnose. Decompose
    to base letter plus accent command where possible, otherwise fall back to
    the bare base letter.
    """
    import unicodedata
    if ch in _NO_DECOMP:
        return _NO_DECOMP[ch]
    decomp = unicodedata.normalize("NFD", ch)
    if len(decomp) >= 2 and decomp[0].isascii():
        base, marks = decomp[0], decomp[1:]
        for mk in marks:
            cmd = _ACCENT_CMD.get(mk)
            if cmd:
                base = ("\\%s{%s}" % (cmd, base) if cmd.isalpha()
                        else "\\%s%s" % (cmd, base))
        return base
    ascii_only = "".join(c for c in decomp if c.isascii())
    return ascii_only or ""


def _sanitize_tex(t: str) -> str:
    """Map characters pdflatex cannot compile onto safe equivalents."""
    for ch, rep in UNICODE_TEX.items():
        t = t.replace(ch, rep)
    for ch, rep in GREEK_TEX.items():
        t = t.replace(ch, f"${rep}$")
    # control characters (Word footnote/bookmark artefacts) also halt pdflatex
    t = "".join(c for c in t if c in "\n\t" or ord(c) >= 32)
    # anything still non-ASCII would halt pdflatex, so convert it
    out = []
    for ch in t:
        if ord(ch) < 128 or ch in "\n\t":
            out.append(ch)
        else:
            out.append(_fallback_char(ch))
    return "".join(out)


ESC = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
       "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def esc(s: str) -> str:
    if not s:
        return ""
    out = []
    for ch in s:
        out.append(ESC.get(ch, ch))
    t = "".join(out)
    t = t.replace("\\", "\\textbackslash{}") if "\\textbackslash" in t else t
    return t


def _frontmatter(ms: Manuscript, prof: dict) -> str:
    mode = prof["latex"].get("author_mode", "plain")
    L = []
    if mode == "ieee":
        authors = " \\and ".join(
            f"\\IEEEauthorblockN{{{esc(a.name)}}}\\IEEEauthorblockA{{{esc(a.affiliation)}"
            + (f"\\\\{esc(a.email)}" if a.email else "") + "}"
            for a in ms.authors) or "\\IEEEauthorblockN{Author Name}"
        L.append(f"\\title{{{esc(ms.title)}}}")
        L.append(f"\\author{{{authors}}}")
        L.append("\\maketitle")
        L.append("\\begin{abstract}")
        L.append(esc(ms.abstract))
        L.append("\\end{abstract}")
        L.append("\\begin{IEEEkeywords}")
        L.append(", ".join(esc(k) for k in ms.keywords))
        L.append("\\end{IEEEkeywords}")
    elif mode == "elsevier":
        L.append("\\begin{frontmatter}")
        L.append(f"\\title{{{esc(ms.title)}}}")
        for idx, a in enumerate(ms.authors, 1):
            star = "\\corref{cor1}" if a.corresponding or idx == 1 else ""
            L.append(f"\\author[aff{idx}]{{{esc(a.name)}{star}}}")
            if a.email:
                L.append(f"\\ead{{{a.email}}}")
            L.append(f"\\affiliation[aff{idx}]{{organization={{{esc(a.affiliation)}}}}}")
        L.append("\\cortext[cor1]{Corresponding author}")
        L.append("\\begin{abstract}")
        L.append(esc(ms.abstract))
        L.append("\\end{abstract}")
        L.append("\\begin{keyword}")
        L.append(" \\sep ".join(esc(k) for k in ms.keywords))
        L.append("\\end{keyword}")
        L.append("\\end{frontmatter}")
    elif mode == "sage":
        run = ms.title if len(ms.title) <= 60 else ms.title[:57].rsplit(" ", 1)[0] + "..."
        L.append(f"\\runninghead{{{esc(run)}}}")
        L.append(f"\\title{{{esc(ms.title)}}}")
        L.append("\\author{" + ", ".join(esc(a.name) for a in ms.authors) + "}")
        affs = [a.affiliation for a in ms.authors if a.affiliation]
        L.append("\\affiliation{" + esc(affs[0] if affs else "") + "}")
        corr = next((a for a in ms.authors if a.corresponding), None) or \
            (ms.authors[0] if ms.authors else None)
        L.append("\\corrauth{" + (esc(corr.name) if corr else "") + "}")
        L.append("\\email{" + (corr.email if corr and corr.email else "") + "}")
        L.append("\\begin{abstract}")
        L.append(esc(ms.abstract))
        L.append("\\end{abstract}")
        L.append("\\keywords{" + ", ".join(esc(k) for k in ms.keywords) + "}")
        L.append("\\maketitle")
    elif mode == "generic":
        # a class we have no specific knowledge of: standard commands only
        L.append(f"\\title{{{esc(ms.title)}}}")
        L.append("\\author{" + " \\and ".join(esc(a.name) for a in ms.authors) + "}")
        L.append("\\maketitle")
        L.append("\\begin{abstract}")
        L.append(esc(ms.abstract))
        L.append("\\end{abstract}")
        L.append("\\noindent\\textbf{Keywords:} " + ", ".join(esc(k) for k in ms.keywords))
    elif mode == "springer":
        L.append(f"\\title[{esc(ms.title[:60])}]{{{esc(ms.title)}}}")
        for a in ms.authors:
            L.append(f"\\author{{\\fnm{{}}\\sur{{{esc(a.name)}}}}}")
        for idx, a in enumerate(ms.authors, 1):
            if a.affiliation:
                L.append(f"\\affil[{idx}]{{\\orgname{{{esc(a.affiliation)}}}}}")
        L.append("\\abstract{" + esc(ms.abstract) + "}")
        L.append("\\keywords{" + ", ".join(esc(k) for k in ms.keywords) + "}")
        L.append("\\maketitle")
    else:
        L.append(f"\\title{{{esc(ms.title)}}}")
        L.append("\\author{" + " \\and ".join(esc(a.name) for a in ms.authors) + "}")
        L.append("\\date{}")
        L.append("\\maketitle")
        if prof.get("abstract", {}).get("type") == "structured" and ms.structured_abstract:
            L.append("\\begin{abstract}")
            for k, v in ms.structured_abstract.items():
                L.append(f"\\textbf{{{esc(k)}:}} {esc(v)}\n")
            L.append("\\end{abstract}")
        else:
            L.append("\\begin{abstract}")
            L.append(esc(ms.abstract))
            L.append("\\end{abstract}")
        L.append("\\noindent\\textbf{Keywords:} " + ", ".join(esc(k) for k in ms.keywords))
    return "\n".join(L)


MATH_TOKEN = re.compile("\u27eaMATH(\\d+)\u27eb")


def _col_fracs(rows: list[list[str]], ncol: int) -> list[float]:
    """Column widths proportional to content, damped so prose doesn't dominate.

    Mirrors the Word renderer, so a narrow 'Sl.no' column doesn't get the same
    space as a paragraph of methodology text.
    """
    rows = [[inline_math.to_plain(c) for c in r] for r in rows]
    widths, min_need = [], []
    for ci in range(ncol):
        longest_cell, longest_word = 1, 1
        for r in rows:
            if ci >= len(r):
                continue
            cell = r[ci]
            longest_cell = max(longest_cell, len(cell))
            for tok in cell.split():
                longest_word = max(longest_word, len(tok))
        widths.append(min(longest_cell, 40) ** 0.6)
        # a column narrower than its longest unbreakable word overflows no
        # matter how the rest is balanced, so record that floor
        min_need.append(min(longest_word, 22))
    total = sum(widths) or 1.0
    fracs = [x / total for x in widths]
    floor_total = sum(min_need) or 1.0
    floors = [m / floor_total for m in min_need]
    fracs = [max(f, fl * 0.95) for f, fl in zip(fracs, floors)]
    t2 = sum(fracs) or 1.0
    return [f / t2 for f in fracs]


def _breakable(cell: str) -> str:
    r"""Let long tokens in a table cell wrap.

    Cells like "source(host/network/both/flow based)" contain no space, so TeX
    cannot break them and the text runs past the column edge — which is what
    gets cropped at the page margin. Adding \allowbreak after the natural
    punctuation gives it somewhere to break without changing the text.
    """
    out = re.sub(r"([/(),;:])", r"\1\\allowbreak{}", cell)
    # a very long run with no punctuation at all still needs breakpoints
    def _chop(m):
        w = m.group(0)
        return "\\allowbreak{}".join(w[i:i + 12] for i in range(0, len(w), 12))
    return re.sub(r"[A-Za-z0-9\\-]{22,}", _chop, out)


def _cell_tex(cell: str) -> str:
    """Table cell: escaped, breakable prose with maths kept as $...$.

    _breakable must not run inside the maths, or \\allowbreak lands inside
    $\\alpha$ and breaks the compile.
    """
    if not inline_math.has_math(cell):
        return _breakable(esc(cell))
    return inline_math.to_latex(cell, lambda s: _breakable(esc(s)))


def _tabular_spec(rows: list[list[str]], ncol: int) -> str:
    r"""Column specification whose total width is exactly \linewidth.

    The trap: a p{0.13\linewidth} column is 0.13\linewidth *plus* 2\tabcolsep
    of padding. Seven of those overrun the line by ~84pt, and the table prints
    past the margin and gets cropped. Subtracting the padding inside \dimexpr
    makes the arithmetic exact regardless of column count.
    """
    fracs = _col_fracs(rows, ncol)
    cells = []
    for f in fracs:
        cells.append(
            ">{\\RaggedRight\\arraybackslash}"
            "p{\\dimexpr %.4f\\linewidth-2\\tabcolsep\\relax}" % f)
    return "".join(cells)


def _para_tex(block) -> str:
    """Escape body text, then put inline equations back as real LaTeX math."""
    eqs = getattr(block, "inline_eqs", None) or []
    if not eqs:
        return esc(block.text)
    out, pos = [], 0
    for m in MATH_TOKEN.finditer(block.text):
        out.append(esc(block.text[pos:m.start()]))
        idx = int(m.group(1))
        if idx < len(eqs):
            out.append("$" + eqs[idx].get("latex", "") + "$")
        pos = m.end()
    out.append(esc(block.text[pos:]))
    return "".join(out)


def _body(ms: Manuscript, prof: dict, float_mode: str = "journal") -> str:
    figs = {f.label: f for f in ms.figures}
    tabs = {t.label: t for t in ms.tables}
    eqs = {e.label: e for e in ms.equations}
    cmd = {1: "section", 2: "subsection", 3: "subsubsection"}
    L = []
    for sec in ms.sections:
        L.append(f"\\{cmd.get(sec.level, 'subsubsection')}{{{esc(sec.title)}}}")
        for b in sec.blocks:
            if b.kind == "para":
                L.append(_para_tex(b) + "\n")
            elif b.kind == "equation" and b.ref in eqs:
                e = eqs[b.ref]
                if getattr(e, "image_path", "") and not e.latex:
                    L.append("\\begin{equation}\n\\includegraphics[width=0.7"
                             "\\linewidth]{figures/%s}\n\\end{equation}\n"
                             % os.path.basename(e.image_path))
                    continue
                L.append("\\begin{equation}")
                L.append(e.latex)
                L.append(f"\\label{{{e.label}}}")
                L.append("\\end{equation}\n")
            elif b.kind == "figure" and b.ref in figs:
                f = figs[b.ref]
                fplace = "[H]" if float_mode == "inline" else "[!htbp]"
                L.append(f"\\begin{{figure}}{fplace}\n\\centering")
                if f.path:
                    img = os.path.basename(f.path)
                    L.append(f"\\includegraphics[width={f.width_frac}"
                             f"\\linewidth]{{figures/{img}}}")
                else:
                    # No artwork in the source. Emitting \includegraphics for a
                    # file that does not exist halts the compile, so draw a
                    # visible placeholder the author cannot miss instead.
                    L.append("\\fbox{\\parbox[c][2.2cm][c]{0.85\\linewidth}"
                             "{\\centering \\textit{[figure artwork missing "
                             "from the source file]}}}")
                cap = inline_math.to_latex(f.caption, esc) if f.caption else ""
                L.append(f"\\caption{{{cap}}}\n\\label{{{f.label}}}\n"
                         "\\end{figure}\n")
            elif b.kind == "table" and b.ref in tabs:
                t = tabs[b.ref]
                ncol = max((len(r) for r in t.rows), default=2)
                two_col = prof["word"]["columns"] > 1
                wide = two_col and ncol >= 4
                env = "table*" if wide else "table"
                # a starred float in a two-column class can only go top or on a
                # float page; an ordinary one may also sit here or at the bottom
                if float_mode == "inline":
                    # H (from the float package) pins it exactly here; a starred
                    # float cannot be pinned, so a wide table stays a real float
                    place = "[!tb]" if wide else "[H]"
                else:
                    place = "[!tb]" if wide else "[!htbp]"
                # p{} columns so long cell text wraps instead of overflowing
                spec = (_tabular_spec(t.rows, ncol) if ncol >= 3
                        else "l" * ncol)
                L.append(f"\\begin{{{env}}}{place}\n\\centering")
                L.append(f"\\caption{{{inline_math.to_latex(t.caption, esc)}}}"
                         f"\n\\label{{{t.label}}}")
                if ncol >= 6:
                    L.append("\\setlength{\\tabcolsep}{3pt}\\scriptsize")
                elif ncol >= 4:
                    L.append("\\setlength{\\tabcolsep}{4pt}\\footnotesize")
                elif wide:
                    L.append("\\footnotesize")
                L.append("\\begin{tabular}{" + spec + "}\n\\hline")
                for ri, row in enumerate(t.rows):
                    cells = [_cell_tex(c) for c in row] + \
                            [""] * (ncol - len(row))
                    if ri == 0:
                        cells = [f"\\textbf{{{c}}}" for c in cells]
                    L.append(" & ".join(cells) + " \\\\")
                    if ri == 0:
                        L.append("\\hline")
                L.append(f"\\hline\n\\end{{tabular}}\n\\end{{{env}}}\n")
    return "\n".join(L)


def _bibliography(ms: Manuscript, prof: dict) -> str:
    if not ms.references:
        return ""
    L = ["\\begin{thebibliography}{99}"]
    for i, r in enumerate(ms.references, 1):
        L.append(f"\\bibitem{{ref{i}}} {esc(r.raw)}")
    L.append("\\end{thebibliography}")
    return "\n".join(L)


def _strip_tokens(t: str) -> str:
    """Last-resort guard.

    A placeholder that survives to the output is both visible junk and, in
    LaTeX, a hard compile error — pdflatex cannot handle U+27EA/U+27EB and
    Overleaf stops with "Unicode character not set up for use with LaTeX".
    Nothing should reach here, but if it does, drop it rather than ship a
    file that will not compile.
    """
    return re.sub("\u27eaMATH\\d+\u27eb", "", t)


COMPACT_PREAMBLE = [
    "% --- layout compaction: fewer pages, not fewer words ------------------",
    "\\usepackage{microtype}",
    "\\usepackage[font=small,skip=3pt]{caption}",
    "\\setlength{\\textfloatsep}{6pt plus 2pt minus 2pt}",
    "\\setlength{\\floatsep}{5pt plus 2pt minus 2pt}",
    "\\setlength{\\intextsep}{5pt plus 2pt minus 2pt}",
    "\\setlength{\\dbltextfloatsep}{6pt plus 2pt minus 2pt}",
    "\\setlength{\\abovedisplayskip}{3pt}",
    "\\setlength{\\belowdisplayskip}{3pt}",
    "\\setlength{\\abovedisplayshortskip}{2pt}",
    "\\setlength{\\belowdisplayshortskip}{2pt}",
    "\\usepackage{enumitem}",
    "\\setlist{nosep,topsep=2pt,partopsep=0pt}",
    "\\usepackage{titlesec}",
    "\\titlespacing*{\\section}{0pt}{6pt plus 2pt minus 2pt}{3pt}",
    "\\titlespacing*{\\subsection}{0pt}{5pt plus 1pt minus 1pt}{2pt}",
    "\\usepackage{balance}",
]


def render(ms: Manuscript, prof: dict, fallback_class: bool = False,
           float_mode: str = "journal", compact: bool = False) -> str:
    """float_mode:
      "journal" — normal LaTeX floats. Tables and figures move to the top or
          bottom of a page. This is what publishers expect and what a typeset
          paper looks like, but it will not match the Word preview page for page.
      "inline"  — every float pinned exactly where it appears in the text, so
          the PDF matches the preview. Leaves more whitespace and can push a
          wide table past the page break, but nothing wanders.
    """
    lx = prof["latex"]
    if fallback_class:
        # stock classes only — compiles on any TeX install, including Overleaf,
        # without the publisher's .cls
        w = prof["word"]
        opts = ["twocolumn"] if w.get("columns", 1) > 1 else []
        opts.append(f"{int(round(w.get('body_size', 10)))}pt")
        opts.append("a4paper" if w.get("page_size") == "a4" else "letterpaper")
        m = w.get("margins_in", {"top": 1, "bottom": 1, "left": 1, "right": 1})
        pre = ["\\documentclass[" + ",".join(opts) + "]{article}",
               "\\usepackage{graphicx}", "\\usepackage{amsmath,amssymb}",
               "\\usepackage{url}", "\\usepackage{booktabs}",
               "\\usepackage{stfloats}", "\\usepackage{float}",
               "\\usepackage{array}", "\\usepackage{ragged2e}",
               "% long unbroken tokens in table headers would otherwise stick",
               "% out past the column and be cropped at the page edge",
               "\\emergencystretch=3em", "\\renewcommand{\\arraystretch}{1.2}",
               "% Float placement: LaTeX defers a float it cannot fit, and a",
               "% review paper with 30+ floats queues them all to the end, so the",
               "% text ends up discussing tables that appear pages later. Relax",
               "% the limits and allow bottom placement so they land near their",
               "% mention.",
               "\\setcounter{topnumber}{4}",
               "\\setcounter{bottomnumber}{3}",
               "\\setcounter{totalnumber}{8}",
               "\\setcounter{dbltopnumber}{4}",
               "\\renewcommand{\\topfraction}{0.9}",
               "\\renewcommand{\\bottomfraction}{0.8}",
               "\\renewcommand{\\textfraction}{0.07}",
               "\\renewcommand{\\floatpagefraction}{0.6}",
               "\\renewcommand{\\dbltopfraction}{0.9}",
               "\\renewcommand{\\dblfloatpagefraction}{0.6}",
               "\\usepackage[top=%gin,bottom=%gin,left=%gin,right=%gin]{geometry}"
               % (m["top"], m["bottom"], m["left"], m["right"]),
               "\\usepackage{setspace}",
               "\\usepackage{caption}", "\\captionsetup{font=small}"]
        ls = w.get("line_spacing", 1.0)
        if ls and float(ls) >= 1.9:
            pre.append("\\doublespacing")
        elif ls and float(ls) >= 1.4:
            pre.append("\\onehalfspacing")
        prof = {**prof, "latex": {**lx, "author_mode": "plain"}}
    else:
        pre = [lx["documentclass"], "\\usepackage{graphicx}",
               "\\usepackage{amsmath,amssymb}",
               "\\usepackage{url}", "\\usepackage{booktabs}",
               "\\usepackage{stfloats}", "\\usepackage{float}",
               "\\usepackage{array}", "\\usepackage{ragged2e}",
               "% long unbroken tokens in table headers would otherwise stick",
               "% out past the column and be cropped at the page edge",
               "\\emergencystretch=3em", "\\renewcommand{\\arraystretch}{1.2}",
               "% Float placement: LaTeX defers a float it cannot fit, and a",
               "% review paper with 30+ floats queues them all to the end, so the",
               "% text ends up discussing tables that appear pages later. Relax",
               "% the limits and allow bottom placement so they land near their",
               "% mention.",
               "\\setcounter{topnumber}{4}",
               "\\setcounter{bottomnumber}{3}",
               "\\setcounter{totalnumber}{8}",
               "\\setcounter{dbltopnumber}{4}",
               "\\renewcommand{\\topfraction}{0.9}",
               "\\renewcommand{\\bottomfraction}{0.8}",
               "\\renewcommand{\\textfraction}{0.07}",
               "\\renewcommand{\\floatpagefraction}{0.6}",
               "\\renewcommand{\\dbltopfraction}{0.9}",
               "\\renewcommand{\\dblfloatpagefraction}{0.6}",]
        if lx.get("author_mode") == "plain":
            pre += ["\\usepackage[margin=1in]{geometry}", "\\usepackage{setspace}",
                    "\\doublespacing"]
    if compact:
        pre = pre + COMPACT_PREAMBLE
    doc = "\n".join(pre) + "\n\n\\begin{document}\n\n" + \
        _frontmatter(ms, prof) + "\n\n" + _body(ms, prof, float_mode) + "\n\n" + \
        _bibliography(ms, prof) + "\n\n\\end{document}\n"
    return _sanitize_tex(_strip_tokens(doc))


# Classes preinstalled on Overleaf and in TeX Live, so main.tex can use them
# directly without shipping the .cls in the zip.
# Classes verified present in a standard TeX Live / Overleaf installation.
# Anything not on this list must be supplied by the user, or main.tex falls
# back to `article` — an optimistic entry here means a failed Overleaf build,
# so each one is checked against a real installation rather than assumed.
STOCK_CLASSES = {
    "ieeetran.cls", "elsarticle.cls", "llncs.cls", "acmart.cls",
    "revtex4-2.cls", "revtex4-1.cls", "article.cls", "amsart.cls",
    "achemso.cls", "aa.cls",
}


def _class_available(prof: dict, extra: dict[str, bytes] | None) -> bool:
    """True when main.tex can name the publisher class directly."""
    want = (prof.get("latex", {}).get("class_file") or "").lower()
    if not want:
        return True
    if want in STOCK_CLASSES:
        return True                     # Overleaf/TeX Live already has it
    return any(os.path.basename(k).lower() == want for k in (extra or {}))


def build_zip(ms: Manuscript, prof: dict,
              extra_files: dict[str, bytes] | None = None,
              standalone: bool = True,
              float_mode: str = "journal",
              compact: bool = False) -> bytes:
    """Return a .zip containing main.tex, figures/ and any supplied class files.

    extra_files: name -> bytes, typically the publisher's .cls/.sty/.bst taken
        from the template kit the user uploaded. Without the class file Overleaf
        stops immediately with "File `IEEEtran.cls' not found", which is the
        single most common reason a generated project fails to build.
    standalone: when the class file is not available, fall back to a stock
        `article` preamble that approximates the layout, so the project always
        compiles. The publisher class can be dropped in later.
    """
    extra_files = extra_files or {}
    have_class = _class_available(prof, extra_files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("main.tex", render(ms, prof,
                                      fallback_class=not have_class and standalone,
                                      float_mode=float_mode, compact=compact))
        for name, data in extra_files.items():
            z.writestr(os.path.basename(name), data)
        for f in ms.figures:
            if f.path and os.path.exists(f.path):
                z.write(f.path, f"figures/{os.path.basename(f.path)}")
        for e in ms.equations:
            ip = getattr(e, "image_path", "")
            if ip and os.path.exists(ip):
                z.write(ip, f"figures/{os.path.basename(ip)}")
        cls = prof["latex"].get("class_file")
        # the README must describe the class main.tex really uses
        dm = re.search(r"\{([^}]+)\}\s*$", prof["latex"].get("documentclass", ""))
        if have_class and dm:
            cls = dm.group(1) + ".cls"
        elif not have_class and not cls and dm:
            cls = dm.group(1) + ".cls"
        bundled = [n for n in extra_files if n.lower().endswith(".cls")]
        if have_class:
            where = ("included in this zip" if bundled
                     else "already installed on Overleaf and in TeX Live")
            note = (f"The class ({cls}) is {where}, so this project compiles\n"
                    "as-is: upload the zip to Overleaf, set main.tex as the main\n"
                    "document, and press Recompile.\n")
        else:
            note = (f"The publisher class ({cls}) was NOT available, so main.tex\n"
                    "uses a stock `article` preamble that approximates the layout\n"
                    "and compiles anywhere. To get the exact journal style, add\n"
                    f"{cls} to this folder and change the first line back to:\n"
                    f"    {prof['latex'].get('documentclass','')}\n")
        notes = prof['latex'].get('notes', '')
        if cls and notes and re.search(r"\b\w+\.cls\b", notes) and cls.lower() not in notes.lower():
            notes = ""          # notes written for a different class would mislead
        z.writestr("README.txt",
                   f"Target: {prof['name']} ({prof['publisher']})\n"
                   f"Class file: {cls or 'none (plain article)'}\n\n"
                   f"{note}\n{notes}\n")
    return buf.getvalue()
