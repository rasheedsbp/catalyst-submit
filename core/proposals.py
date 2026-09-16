"""Turn detected problems into individually approvable fixes.

Nothing here changes a manuscript on its own. Each proposal carries a plain
description of what it would do and a function that does it, and the app applies
only the ones the user ticks.

Two rules govern what may be proposed:

1. **Only deterministic, reversible edits.** Renumbering, reordering, trimming a
   keyword list, dropping a dangling citation — things with one obviously correct
   result. Rewriting prose to satisfy a reviewer is not proposed, because doing it
   offline would mean guessing at the author's meaning.
2. **Nothing touching scientific content.** Numbers, claims, results and
   references are never edited. A proposal may reorder or renumber them; it may
   not change what they say.

Reviewer comments found in the .docx are surfaced as advisory items. Where a
comment maps onto something mechanical ("add two more keywords", "abstract is
over the limit") it is linked to the matching proposal, but the judgement stays
with the user.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .model import Manuscript


@dataclass
class Proposal:
    id: str
    title: str                      # one line, what it does
    detail: str                     # why, and exactly what changes
    severity: str                   # fail | warn | info
    apply: Callable[[Manuscript], Manuscript]
    default: bool = False           # pre-ticked?
    category: str = "content"       # content | numbering | template
    linked_comment: str = ""        # reviewer comment this answers


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _body_text(ms: Manuscript) -> str:
    return " ".join(b.text for s in ms.sections for b in s.blocks
                    if b.kind == "para")


def _match_comment(ms: Manuscript, *keywords: str) -> str:
    for c in ms.comments or []:
        low = c["text"].lower()
        if all(k in low for k in keywords):
            return f"{c['author']}: {c['text']}"
    return ""


# --------------------------------------------------------------------------- #
def build(ms: Manuscript, prof: dict) -> list[Proposal]:
    out: list[Proposal] = []

    # ---- keywords over the journal maximum --------------------------------
    kw = prof.get("keywords", {})
    kmax = kw.get("max")
    if kmax and len(ms.keywords) > kmax:
        extra = ms.keywords[kmax:]

        def _trim(m: Manuscript, n=kmax) -> Manuscript:
            m.keywords = m.keywords[:n]
            return m

        out.append(Proposal(
            id="kw_trim",
            title=f"Trim keywords to the journal's maximum of {kmax}",
            detail=f"Keeps the first {kmax} and drops: {', '.join(extra)}. "
                   "Reorder them yourself first if the ones to drop aren't last.",
            severity="fail", apply=_trim, default=False, category="content",
            linked_comment=_match_comment(ms, "keyword")))

    # ---- figures renumbered out of order ----------------------------------
    fig_nums = [int(re.sub(r"\D", "", f.label) or 0) for f in ms.figures]
    if fig_nums and fig_nums != sorted(fig_nums):
        def _renum_figs(m: Manuscript) -> Manuscript:
            mapping = {}
            for i, f in enumerate(m.figures, 1):
                mapping[f.label] = f"fig{i}"
            for s in m.sections:
                for b in s.blocks:
                    if b.kind == "figure" and b.ref in mapping:
                        b.ref = mapping[b.ref]
            for i, f in enumerate(m.figures, 1):
                f.label = f"fig{i}"
            return m

        out.append(Proposal(
            id="fig_renumber",
            title="Renumber figures sequentially in document order",
            detail=f"Current order is {fig_nums}. In-text references like "
                   "\"Fig. 7\" will no longer match, so only accept this if the "
                   "author's numbering is genuinely wrong.",
            severity="warn", apply=_renum_figs, default=False,
            category="numbering"))

    # ---- tables renumbered out of order -----------------------------------
    tab_nums = [int(re.sub(r"\D", "", t.label) or 0) for t in ms.tables]
    if tab_nums and tab_nums != sorted(tab_nums):
        def _renum_tabs(m: Manuscript) -> Manuscript:
            mapping = {t.label: f"tab{i}" for i, t in enumerate(m.tables, 1)}
            for s in m.sections:
                for b in s.blocks:
                    if b.kind == "table" and b.ref in mapping:
                        b.ref = mapping[b.ref]
            for i, t in enumerate(m.tables, 1):
                t.label = f"tab{i}"
            return m

        out.append(Proposal(
            id="tab_renumber",
            title="Renumber tables sequentially in document order",
            detail=f"Current order is {tab_nums}. Same caveat as figures: "
                   "in-text references will shift.",
            severity="warn", apply=_renum_tabs, default=False,
            category="numbering"))

    # ---- equations renumbered ---------------------------------------------
    if ms.equations:
        nums = [e.number for e in ms.equations]
        if nums != list(range(1, len(nums) + 1)):
            def _renum_eqs(m: Manuscript) -> Manuscript:
                for i, e in enumerate(m.equations, 1):
                    e.number = i
                return m

            out.append(Proposal(
                id="eq_renumber",
                title="Renumber equations sequentially",
                detail=f"Current numbers: {nums}. Accept only if the author's "
                       "own numbering is wrong — in-text references use it.",
                severity="info", apply=_renum_eqs, default=False,
                category="numbering"))

    # ---- dangling citations ------------------------------------------------
    body = _body_text(ms)
    cited = {int(x) for x in re.findall(r"\[(\d{1,3})\]", body)}
    dangling = sorted(c for c in cited if c > len(ms.references))
    if dangling and ms.references:
        out.append(Proposal(
            id="cite_report",
            title=f"Flag {len(dangling)} citations pointing past the reference list",
            detail=f"Citations {dangling} have no matching entry (the list has "
                   f"{len(ms.references)}). Nothing is deleted — this adds a note "
                   "at the end of the reference list so you can't miss it. Only "
                   "the author can supply the missing sources.",
            severity="fail", category="content", default=False,
            apply=lambda m, d=dangling: _append_note(
                m, f"[FORMATTER NOTE] Citations {d} have no matching reference "
                   "entry — please supply the missing sources.")))

    # ---- figures with no artwork ------------------------------------------
    missing = [f.label for f in ms.figures if not f.path]
    if missing:
        out.append(Proposal(
            id="fig_missing_note",
            title=f"Add a visible placeholder for {len(missing)} figures with no image",
            detail="These captions have no artwork in the source file. The "
                   "placeholder makes the gap obvious in the output instead of "
                   "silently producing a caption with nothing above it.",
            severity="fail", category="content", default=True,
            apply=lambda m: _mark_missing_figures(m)))

    # ---- uncaptioned tables ------------------------------------------------
    nocap = [t.label for t in ms.tables if not t.caption]
    if nocap:
        out.append(Proposal(
            id="tab_caption_note",
            title=f"Insert '[caption required]' on {len(nocap)} uncaptioned tables",
            detail="Most journals reject tables without a caption. This inserts a "
                   "marker you can fill in — it does not invent wording.",
            severity="warn", category="content", default=False,
            apply=lambda m: _mark_missing_captions(m)))

    # ---- abstract over the limit ------------------------------------------
    ab_cfg = prof.get("abstract", {})
    cap = ab_cfg.get("max_words")
    n_words = len(ms.abstract.split())
    if cap and n_words > cap and ab_cfg.get("type") != "structured":
        out.append(Proposal(
            id="abstract_note",
            title=f"Abstract is {n_words} words against a {cap}-word limit",
            detail="Not auto-trimmed: cutting an abstract at a word count "
                   f"destroys its argument. This flags the {n_words - cap} words "
                   "to remove. Shorten it in the Abstract box above and reformat.",
            severity="fail", category="content", default=False,
            apply=lambda m: m,
            linked_comment=_match_comment(ms, "abstract")))

    return out


# --------------------------------------------------------------------------- #
def _append_note(ms: Manuscript, text: str) -> Manuscript:
    from .model import Reference
    ms.references.append(Reference(raw=text))
    return ms


def _mark_missing_figures(ms: Manuscript) -> Manuscript:
    for f in ms.figures:
        if not f.path and "[artwork missing]" not in f.caption:
            f.caption = (f.caption + "  [artwork missing from source]").strip()
    return ms


def _mark_missing_captions(ms: Manuscript) -> Manuscript:
    for t in ms.tables:
        if not t.caption:
            t.caption = "[caption required]"
    return ms


def apply_selected(ms: Manuscript, proposals: list[Proposal],
                   accepted: set[str]) -> tuple[Manuscript, list[str]]:
    """Apply only the accepted proposals. Returns (manuscript, applied titles)."""
    done = []
    for p in proposals:
        if p.id in accepted:
            ms = p.apply(ms)
            done.append(p.title)
    return ms, done
