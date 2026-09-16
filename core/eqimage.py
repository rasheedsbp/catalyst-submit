"""Render LaTeX equations to PNG so they are visible everywhere.

Word can display re-injected OMML natively, but LibreOffice's OMML import is
unreliable — equations come out blank in the PDF preview and in any PDF sent to
a client. Rasterising the LaTeX sidesteps that entirely: an image renders the
same in Word, LibreOffice, Google Docs and PDF.

Trade-off: an image is not editable in Word. `render_docx` therefore supports
both modes and defaults to images, because a visible equation the author must
retype beats an invisible one they never notice.

Uses matplotlib's mathtext (no TeX installation required). Falls back to None
when the expression uses constructs mathtext cannot parse, and the caller then
writes the LaTeX as plain text.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile

_CACHE: dict[str, str | None] = {}
_DIR = None

# mathtext understands a useful subset of LaTeX; smooth over the common gaps
_FIXES = [
    (r"\\left\s*\.", r"\\left."),
    (r"\\right\s*\.", r"\\right."),
    (r"\\begin\{aligned\}", ""),
    (r"\\end\{aligned\}", ""),
    # Do not rewrite matrix environments into brackets: that can change the
    # mathematical structure. Unsupported constructs simply fall back to OMML.

    (r"\\operatorname\{([^}]*)\}", r"\\mathrm{\1}"),
    (r"\\boxed\{", r"{"),
    (r"\\phantom\{[^}]*\}", " "),
    (r"\\,", " "),
    (r"\\;", " "),
    (r"&", " "),
    (r"\\\\", " "),
]


def _prep(latex: str) -> str:
    t = latex.strip()
    for pat, rep in _FIXES:
        t = re.sub(pat, rep, t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _outdir() -> str:
    global _DIR
    if _DIR is None:
        _DIR = tempfile.mkdtemp(prefix="jf_eq_")
    return _DIR


def render(latex: str, pt: float = 11, dpi: int = 300) -> str | None:
    """Return a PNG path for this equation, or None if it cannot be rendered."""
    if not latex or not latex.strip():
        return None
    key = hashlib.md5(f"{latex}|{pt}|{dpi}".encode()).hexdigest()
    if key in _CACHE:
        return _CACHE[key]

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        _CACHE[key] = None
        return None

    expr = _prep(latex)
    # MathText is intentionally a subset of LaTeX. Refuse constructs that it
    # commonly mis-renders instead of producing a visually plausible but wrong
    # equation. The DOCX renderer will then preserve the original OMML.
    unsafe = (r"\begin{", r"\end{", r"\substack", r"\overset",
              r"\underset", r"\cases", r"\tag", r"\label")
    if any(tok in expr for tok in unsafe):
        _CACHE[key] = None
        return None
    path = os.path.join(_outdir(), f"eq_{key}.png")
    for attempt in (expr, re.sub(r"\\left|\\right", "", expr)):
        try:
            fig = plt.figure(figsize=(0.01, 0.01))
            fig.text(0, 0, f"${attempt}$", fontsize=pt)
            fig.savefig(path, dpi=dpi, transparent=True,
                        bbox_inches="tight", pad_inches=0.02)
            plt.close(fig)
            if os.path.getsize(path) > 0:
                _CACHE[key] = path
                return path
        except Exception:
            try:
                plt.close("all")
            except Exception:
                pass
            continue
    _CACHE[key] = None
    return None


def size_inches(path: str) -> tuple[float, float] | None:
    """Physical size for the PNG so its type matches the body text."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return None
    # rendered at 300 dpi against the requested point size
    return (w / 300.0, h / 300.0)
