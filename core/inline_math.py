"""Inline maths inside captions and table cells.

Paragraph text carries equations as ⟪MATHn⟫ placeholders plus a side list, but
captions and table cells are plain strings. Before this module they were built
from python-docx's ``.text``, which skips m:oMath entirely — so a header like
"RMSE(α)" arrived as "RMSE()" and a caption "for α and θ" as "for  and ".

Here the maths is kept *inside* the string as ⟦latex⟧ and each renderer turns it
into its own syntax: real $...$ in LaTeX, readable Unicode in Word/HTML.
"""
from __future__ import annotations

import re

OPEN, CLOSE = "\u27e6", "\u27e7"
TOKEN = re.compile(OPEN + r"(.*?)" + CLOSE, re.S)
PLACEHOLDER = re.compile("\u27eaMATH(\\d+)\u27eb")


def wrap(tex: str) -> str:
    tex = (tex or "").strip()
    return f"{OPEN}{tex}{CLOSE}" if tex else ""


def from_placeholders(text: str, inline_eqs: list) -> str:
    """Turn ⟪MATHn⟫ + side list into self-contained ⟦latex⟧ tokens."""
    def rep(m):
        i = int(m.group(1))
        return wrap(inline_eqs[i].get("latex", "")) if i < len(inline_eqs) else ""
    return PLACEHOLDER.sub(rep, text or "")


def has_math(s: str) -> bool:
    return bool(s) and OPEN in s


def to_latex(s: str, esc) -> str:
    """Escape the prose with ``esc`` and emit the maths as $...$."""
    if not s:
        return ""
    out, pos = [], 0
    for m in TOKEN.finditer(s):
        out.append(esc(s[pos:m.start()]))
        out.append("$" + m.group(1) + "$")
        pos = m.end()
    out.append(esc(s[pos:]))
    return "".join(out)


_SYM = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\theta": "θ", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ",
    r"\tau": "τ", r"\phi": "φ", r"\omega": "ω", r"\Gamma": "Γ",
    r"\Delta": "Δ", r"\Sigma": "Σ", r"\Omega": "Ω", r"\Phi": "Φ",
    r"\neq": "≠", r"\ne": "≠", r"\leq": "≤", r"\le": "≤", r"\geq": "≥",
    r"\ge": "≥", r"\times": "×", r"\pm": "±", r"\approx": "≈",
    r"\infty": "∞", r"\in": "∈", r"\rightarrow": "→", r"\to": "→",
    r"\cdot": "·", r"\sum": "Σ", r"\prod": "Π", r"\partial": "∂",
}


def tex_to_plain(tex: str) -> str:
    """Readable approximation of simple inline LaTeX, for Word/HTML."""
    t = tex or ""
    t = re.sub(r"\\(?:text|mathrm|mathit|mathbf|operatorname)\{([^{}]*)\}", r"\1", t)
    for k in sorted(_SYM, key=len, reverse=True):
        t = re.sub(re.escape(k) + r"(?![A-Za-z])", _SYM[k], t)
    t = re.sub(r"\\left|\\right", "", t)
    t = re.sub(r"\\[,;:! ]", " ", t)
    t = re.sub(r"\\([A-Za-z]+)", r"\1", t)
    t = t.replace("{", "").replace("}", "")
    t = re.sub(r"\s*([=<>≠≤≥])\s*", r" \1 ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def to_plain(s: str) -> str:
    """Strip tokens to readable text (Word, HTML preview, search, checks)."""
    if not s:
        return ""
    return TOKEN.sub(lambda m: tex_to_plain(m.group(1)), s)
