"""Apply the user's manual corrections to a parsed Manuscript.

No parser can be right on every client document — Word files simply do not carry
enough information to distinguish, say, an equation pasted as a picture from a
figure. Rather than pretend otherwise, the app shows every detected object and
lets the user correct it. This module applies those corrections.

Corrections are keyed by object label so they survive re-parsing.
"""
from __future__ import annotations

from .model import Manuscript, Figure, Table, Equation, Block


def summarise(ms: Manuscript) -> list[dict]:
    """Every figure/table/equation, in document order, for the review table."""
    figs = {f.label: f for f in ms.figures}
    tabs = {t.label: t for t in ms.tables}
    eqs = {e.label: e for e in ms.equations}
    rows, n = [], 0
    for sec in ms.sections:
        for b in sec.blocks:
            if b.kind not in ("figure", "table", "equation"):
                continue
            n += 1
            item = {"order": n, "label": b.ref, "kind": b.kind,
                    "section": sec.title, "caption": "", "detail": "",
                    "has_image": False}
            if b.kind == "figure" and b.ref in figs:
                f = figs[b.ref]
                item["caption"] = f.caption
                item["has_image"] = bool(f.path)
                item["detail"] = "image present" if f.path else "NO IMAGE in source"
            elif b.kind == "table" and b.ref in tabs:
                t = tabs[b.ref]
                item["caption"] = t.caption
                ncol = max((len(r) for r in t.rows), default=0)
                item["detail"] = f"{len(t.rows)} rows x {ncol} cols"
            elif b.kind == "equation" and b.ref in eqs:
                e = eqs[b.ref]
                item["caption"] = f"({e.number})" if e.number else ""
                item["detail"] = (e.latex[:60] + "…") if len(e.latex) > 60 else e.latex
            rows.append(item)
    return rows


def apply(ms: Manuscript, edits: dict) -> Manuscript:
    """Apply corrections.

    edits maps label -> {"kind": new kind or "drop", "caption": str}.
    Reclassifying a figure as an equation (the common case for equations pasted
    as pictures) keeps the image and renders it without figure numbering.
    """
    if not edits:
        return ms

    figs = {f.label: f for f in ms.figures}
    tabs = {t.label: t for t in ms.tables}
    eqs = {e.label: e for e in ms.equations}

    new_figs: list[Figure] = []
    new_tabs: list[Table] = []
    new_eqs: list[Equation] = []

    for sec in ms.sections:
        kept: list[Block] = []
        for b in sec.blocks:
            if b.kind not in ("figure", "table", "equation"):
                kept.append(b)
                continue
            ed = edits.get(b.ref, {})
            kind = ed.get("kind", b.kind)
            cap = ed.get("caption", None)

            if kind == "drop":
                continue

            if kind == "figure":
                if b.ref in figs:
                    f = figs[b.ref]
                else:  # promoted from a table or equation
                    f = Figure(label=b.ref, caption="", path="")
                    if b.ref in tabs and tabs[b.ref].rows:
                        f.caption = tabs[b.ref].caption
                if cap is not None:
                    f.caption = cap
                f.label = f"fig{len(new_figs) + 1}"
                new_figs.append(f)
                kept.append(Block(kind="figure", ref=f.label))

            elif kind == "table":
                t = tabs.get(b.ref) or Table(label=b.ref, caption="", rows=[])
                if cap is not None:
                    t.caption = cap
                t.label = f"tab{len(new_tabs) + 1}"
                new_tabs.append(t)
                kept.append(Block(kind="table", ref=t.label))

            elif kind == "equation":
                if b.ref in eqs:
                    e = eqs[b.ref]
                else:
                    # a figure the user identified as a pasted equation: keep
                    # the picture, drop the figure numbering
                    src = figs.get(b.ref)
                    e = Equation(label=b.ref, latex="", omml="", number=0)
                    if src and src.path:
                        e.image_path = src.path
                e.label = f"eq{len(new_eqs) + 1}"
                e.number = len(new_eqs) + 1
                new_eqs.append(e)
                kept.append(Block(kind="equation", ref=e.label))
        sec.blocks = kept

    ms.figures, ms.tables, ms.equations = new_figs, new_tabs, new_eqs
    return ms
