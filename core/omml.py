"""Convert Word's native equation markup (OMML) to LaTeX.

Word stores equations as OMML, an XML dialect. python-docx's `.text` ignores it
completely, so equations vanish and sentences lose their symbols — "where <math>
is the real power flow" becomes "where  is the real power flow". Authors who
paste equations as pictures instead hit the other failure: the image gets
mistaken for a figure and numbered alongside the real ones.

This module handles the first case properly. Coverage is the constructs that
actually appear in engineering manuscripts: fractions, sub/superscripts,
radicals, n-ary operators (sum/integral/product), delimiters, matrices,
accents, over/underbars, boxes, and function applications.
"""
from __future__ import annotations

import re

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"

# Unicode maths characters -> LaTeX. Only what Word actually emits.
CHAR_MAP = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\varepsilon", "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi",
    "ρ": r"\rho", "σ": r"\sigma", "ς": r"\varsigma", "τ": r"\tau",
    "υ": r"\upsilon", "φ": r"\varphi", "ϕ": r"\phi", "χ": r"\chi",
    "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi", "Ψ": r"\Psi",
    "Ω": r"\Omega",
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint",
    "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap", "⨁": r"\bigoplus",
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx", "≡": r"\equiv",
    "≜": r"\triangleq", "≅": r"\cong", "∝": r"\propto", "∼": r"\sim",
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div", "⋅": r"\cdot",
    "∙": r"\cdot", "∘": r"\circ", "∗": r"*",
    "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla", "√": r"\sqrt",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊆": r"\subseteq",
    "⊃": r"\supset", "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset",
    "∀": r"\forall", "∃": r"\exists", "¬": r"\neg", "∧": r"\wedge",
    "∨": r"\vee", "⊕": r"\oplus", "⊗": r"\otimes", "∥": r"\|",
    "⌈": r"\lceil", "⌉": r"\rceil", "⌊": r"\lfloor", "⌋": r"\rfloor",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℂ": r"\mathbb{C}", "ℚ": r"\mathbb{Q}", "ℒ": r"\mathcal{L}",
    "…": r"\dots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "°": r"^{\circ}", "′": r"'", "″": r"''", "‖": r"\|",
    "\u00a0": " ", "\u2009": r"\,", "\u2212": "-",
}

ACCENT_MAP = {
    "\u0303": r"\tilde", "~": r"\tilde",
    "\u0302": r"\hat", "^": r"\hat",
    "\u0304": r"\bar", "\u00af": r"\bar",
    "\u0307": r"\dot", "\u0308": r"\ddot",
    "\u20d7": r"\vec", "\u2192": r"\vec",
    "\u0306": r"\breve", "\u030c": r"\check",
}

FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
         "sinh", "cosh", "tanh", "log", "ln", "exp", "lim", "max", "min", "sup",
         "inf", "det", "dim", "arg", "deg", "gcd", "Pr"}

SPECIALS = set("#$%&_{}")


def _esc_text(t: str) -> str:
    out = []
    for ch in t:
        if ch in CHAR_MAP:
            out.append(CHAR_MAP[ch] + " ")
        elif ch in SPECIALS:
            out.append("\\" + ch)
        elif ch == "\\":
            out.append(r"\backslash ")
        else:
            out.append(ch)
    return "".join(out)


def _wrap(s: str) -> str:
    """Brace a sub-expression unless it is already a single token."""
    s = s.strip()
    if not s:
        return "{}"
    if len(s) == 1:
        return s
    if re.fullmatch(r"\\[A-Za-z]+", s):
        return s
    return "{" + s + "}"


def _children(el, *tags):
    for t in tags:
        for c in el.findall(M + t):
            yield c


def _kids(el, tag) -> str:
    node = el.find(M + tag)
    return _node(node) if node is not None else ""


def _node(el) -> str:
    """Recursively convert one OMML element."""
    if el is None:
        return ""
    tag = el.tag.replace(M, "")

    if tag in ("oMath", "oMathPara", "e", "num", "den", "sub", "sup", "lim",
               "deg", "fName"):
        return "".join(_node(c) for c in el)

    if tag == "r":                                   # a run of literal symbols
        raw = "".join(t.text or "" for t in el.findall(M + "t"))
        rpr = el.find(M + "rPr")
        normal = rpr is not None and rpr.find(M + "nor") is not None
        # words typed into an equation ("Number of concordant pairs") lose
        # their spaces in math mode; keep them as upright text
        if normal or re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", raw):
            return r"\text{%s}" % raw.replace("{", r"\{").replace("}", r"\}")
        return "".join(_esc_text(t.text or "") for t in el.findall(M + "t"))

    if tag == "t":
        return _esc_text(el.text or "")

    if tag == "f":                                   # fraction
        pr = el.find(M + "fPr")
        typ = ""
        if pr is not None:
            t = pr.find(M + "type")
            typ = t.get(M + "val", "") if t is not None else ""
        num, den = _kids(el, "num"), _kids(el, "den")
        if typ == "lin":
            return f"{_wrap(num)}/{_wrap(den)}"
        if typ == "noBar":
            return r"\binom{%s}{%s}" % (num, den)
        return r"\frac{%s}{%s}" % (num or "{}", den or "{}")

    if tag == "sSub":
        return f"{_wrap(_kids(el, 'e'))}_{_wrap(_kids(el, 'sub'))}"
    if tag == "sSup":
        return f"{_wrap(_kids(el, 'e'))}^{_wrap(_kids(el, 'sup'))}"
    if tag == "sSubSup":
        return (f"{_wrap(_kids(el, 'e'))}_{_wrap(_kids(el, 'sub'))}"
                f"^{_wrap(_kids(el, 'sup'))}")
    if tag == "sPre":
        return (f"{{}}_{_wrap(_kids(el, 'sub'))}^{_wrap(_kids(el, 'sup'))}"
                f"{_wrap(_kids(el, 'e'))}")

    if tag == "rad":                                 # radical
        deg, e = _kids(el, "deg"), _kids(el, "e")
        return r"\sqrt[%s]{%s}" % (deg, e) if deg.strip() else r"\sqrt{%s}" % e

    if tag == "nary":                                # sum / integral / product
        pr = el.find(M + "naryPr")
        chr_ = "∫"
        hide_sub = hide_sup = False
        if pr is not None:
            c = pr.find(M + "chr")
            if c is not None:
                chr_ = c.get(M + "val", "∫")
            for name, flag in (("subHide", "sub"), ("supHide", "sup")):
                n = pr.find(M + name)
                if n is not None and n.get(M + "val") in ("1", "true", "on"):
                    if flag == "sub":
                        hide_sub = True
                    else:
                        hide_sup = True
        op = CHAR_MAP.get(chr_, r"\int")
        sub, sup = _kids(el, "sub"), _kids(el, "sup")
        out = op
        if sub.strip() and not hide_sub:
            out += "_" + _wrap(sub)
        if sup.strip() and not hide_sup:
            out += "^" + _wrap(sup)
        return out + " " + _kids(el, "e")

    if tag == "d":                                   # delimiters ( ) [ ] { }
        pr = el.find(M + "dPr")
        beg, end, sep = "(", ")", "|"
        if pr is not None:
            for name, var in (("begChr", "beg"), ("endChr", "end"), ("sepChr", "sep")):
                n = pr.find(M + name)
                if n is not None:
                    v = n.get(M + "val", "")
                    if var == "beg":
                        beg = v
                    elif var == "end":
                        end = v
                    else:
                        sep = v
        parts = [_node(e) for e in el.findall(M + "e")]
        body = (sep or "|").join(parts) if len(parts) > 1 else (parts[0] if parts else "")
        lmap = {"": ".", "{": r"\{", "}": r"\}", "|": "|", "⌊": r"\lfloor",
                "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil", "‖": r"\|"}
        b = lmap.get(beg, beg or ".")
        e_ = lmap.get(end, end or ".")
        return r"\left%s %s \right%s" % (b, body, e_)

    if tag == "func":                                # sin, cos, lim …
        name = _kids(el, "fName").strip()
        bare = re.sub(r"[\\{}^_]", "", name)
        cmd = "\\" + bare if bare in FUNCS else r"\operatorname{%s}" % bare
        return f"{cmd} {_wrap(_kids(el, 'e'))}" if bare else _kids(el, "e")

    if tag == "limLow":
        return f"{_kids(el, 'e')}_{_wrap(_kids(el, 'lim'))}"
    if tag == "limUpp":
        return f"{_kids(el, 'e')}^{_wrap(_kids(el, 'lim'))}"

    if tag == "acc":                                 # hat, bar, vec …
        pr = el.find(M + "accPr")
        ch = "\u0302"
        if pr is not None:
            c = pr.find(M + "chr")
            if c is not None:
                ch = c.get(M + "val", "\u0302")
        return f"{ACCENT_MAP.get(ch, r'\hat')}{{{_kids(el, 'e')}}}"

    if tag == "bar":
        pr = el.find(M + "barPr")
        pos = ""
        if pr is not None:
            n = pr.find(M + "pos")
            pos = n.get(M + "val", "") if n is not None else ""
        cmd = r"\underline" if pos == "bot" else r"\overline"
        return f"{cmd}{{{_kids(el, 'e')}}}"

    if tag == "groupChr":
        return f"\\overbrace{{{_kids(el, 'e')}}}"
    if tag == "box" or tag == "borderBox":
        return f"\\boxed{{{_kids(el, 'e')}}}"
    if tag == "phant":
        return f"\\phantom{{{_kids(el, 'e')}}}"

    if tag == "m":                                   # matrix
        rows = []
        for mr in el.findall(M + "mr"):
            rows.append(" & ".join(_node(e) for e in mr.findall(M + "e")))
        return (r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}")

    if tag == "eqArr":                               # aligned equation array
        rows = [_node(e) for e in el.findall(M + "e")]
        return r"\begin{aligned}" + r" \\ ".join(rows) + r"\end{aligned}"

    # unknown wrapper: descend rather than lose the content
    return "".join(_node(c) for c in el)


def omml_to_latex(el) -> str:
    """Convert an <m:oMath> / <m:oMathPara> element to a LaTeX string."""
    tex = _node(el)
    tex = re.sub(r"\s{2,}", " ", tex).strip()
    tex = re.sub(r"\s+([_^])", r"\1", tex)
    return tex


def paragraph_math(p_el) -> list:
    """Every equation in a paragraph, as (element, latex, is_display)."""
    out = []
    for para in p_el.findall(".//" + M + "oMathPara"):
        for om in para.findall(M + "oMath"):
            out.append((om, omml_to_latex(om), True))
    seen = {id(x[0]) for x in out}
    for om in p_el.findall(".//" + M + "oMath"):
        if id(om) not in seen:
            out.append((om, omml_to_latex(om), False))
    return out


def has_math(p_el) -> bool:
    return p_el.find(".//" + M + "oMath") is not None


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def ensure_math_font(node):
    """Give every m:r a Cambria Math w:rPr.

    Word supplies this implicitly, but re-injected OMML without it renders as
    nothing in LibreOffice (and therefore in the PDF preview and any PDF the
    user sends a client). Cheap insurance.
    """
    from lxml import etree
    for r in node.iter(M + "r"):
        if r.find(W_NS + "rPr") is not None:
            continue
        rpr = etree.SubElement(r, W_NS + "rPr")
        fonts = etree.SubElement(rpr, W_NS + "rFonts")
        fonts.set(W_NS + "ascii", "Cambria Math")
        fonts.set(W_NS + "hAnsi", "Cambria Math")
        r.insert(0, rpr)
    return node
