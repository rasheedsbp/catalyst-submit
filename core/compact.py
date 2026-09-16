"""Reduce length without losing information.

There are two very different ways to shorten a paper, and only one of them is
safe for a tool to do unattended.

**Layout compaction — safe, and applied to the LaTeX only.** Tighter float
spacing, smaller table type, reduced caption size, balanced final columns. This
removes *pages* without touching a single word. Nothing the author wrote is
altered, so nothing can be lost.

**Text compaction — not done automatically.** Genuinely shortening prose means
deciding which of an author's sentences carries less value, and rewriting the
rest to cover the gap. Offline, without understanding the field, that is
guesswork applied to a document going out under a researcher's name. What this
module does instead is *find* the redundancy — literal duplicate sentences,
near-duplicates, repeated citation runs, and a per-section word budget — so the
author can cut with full information in minutes rather than hours.

Duplicate removal is the one text edit offered, and only for sentences that are
identical or near-identical, where deleting one provably loses nothing.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .model import Manuscript


# abbreviations whose full stop must not be treated as a sentence end —
# splitting "Fig. 13 illustrates…" after "Fig." produces a fragment that no
# longer looks like a cross-reference, and it could then be deleted as a
# duplicate, silently un-citing the figure
_ABBR = (r"Fig|Figs|Fig\.No|Eq|Eqs|Tab|Tabs|No|Nos|Vol|pp|al|vs|approx|"
         r"Dr|Prof|Mr|Mrs|Ms|St|etc|e\.g|i\.e|cf|Ref|Refs|Sec|Ch|Ed|Eds")


def _sentences(text: str) -> list[str]:
    SENT = "\uE000"
    guarded = re.sub(rf"\b({_ABBR})\.", lambda m: m.group(1) + SENT, text,
                     flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+", guarded)
    return [p.replace(SENT, ".").strip() for p in parts if p.strip()]


def _norm(s: str) -> str:
    s = re.sub(r"\[\d+\]", "", s.lower())
    return re.sub(r"[^a-z0-9 ]", " ", s).strip()


def word_count(ms: Manuscript) -> dict:
    """Words by section, plus the totals a length limit is judged against."""
    per = []
    body = 0
    for sec in ms.sections:
        n = sum(len(b.text.split()) for b in sec.blocks if b.kind == "para")
        per.append({"title": sec.title, "words": n})
        body += n
    return {
        "abstract": len(ms.abstract.split()),
        "body": body,
        "references": len(ms.references),
        "figures": len(ms.figures),
        "tables": len(ms.tables),
        "per_section": sorted(per, key=lambda x: -x["words"]),
    }


REF_RE = re.compile(r"\b(fig(?:ure)?s?\.?\s*\d|tables?\s*[\divx]|eq(?:uation)?s?"
                    r"\.?\s*\(?\d|section\s*\d)", re.I)


def duplicates(ms: Manuscript, threshold: float = 0.92,
               min_words: int = 8) -> list[dict]:
    """Sentences repeated verbatim or near-verbatim elsewhere in the paper.

    Common in review papers assembled section by section — the same framing
    sentence gets pasted into several sections. Removing the later copy loses
    no information.
    """
    items = []
    for si, sec in enumerate(ms.sections):
        for bi, b in enumerate(sec.blocks):
            if b.kind != "para":
                continue
            for s in _sentences(b.text):
                # a sentence carrying a cross-reference is never a safe
                # duplicate: deleting it silently un-cites a figure or table
                if len(s.split()) >= min_words and not REF_RE.search(s):
                    items.append({"sec": si, "block": bi, "text": s,
                                  "norm": _norm(s), "section": sec.title})

    # Comparing every sentence against every earlier one is O(n^2) on strings,
    # which on a 10,000-word review means ~20 seconds — and this runs on every
    # Streamlit rerun, i.e. every keystroke. Block on a cheap word-overlap test
    # first and only run the expensive ratio on plausible pairs.
    seen: dict[str, dict] = {}
    buckets: dict[str, list[str]] = {}      # rare word -> candidate keys
    dupes = []

    def _sig(key: str) -> set[str]:
        words = [w for w in key.split() if len(w) > 4]
        return set(sorted(words, key=len, reverse=True)[:6])

    for it in items:
        key = it["norm"]
        if key in seen:
            dupes.append({"first": seen[key], "repeat": it, "score": 1.0})
            continue

        sig = _sig(key)
        candidates: set[str] = set()
        for w in sig:
            candidates.update(buckets.get(w, ()))

        hit = None
        for k in candidates:
            if abs(len(k) - len(key)) > 40:
                continue
            r = SequenceMatcher(None, k, key).ratio()
            if r >= threshold:
                hit = (seen[k], r)
                break

        if hit:
            dupes.append({"first": hit[0], "repeat": it, "score": hit[1]})
        else:
            seen[key] = it
            for w in sig:
                buckets.setdefault(w, []).append(key)
    return dupes


def remove_duplicates(ms: Manuscript, dupes: list[dict]) -> int:
    """Delete the repeated copies. Returns the number of words saved."""
    saved = 0
    targets: dict[tuple[int, int], list[str]] = {}
    for d in dupes:
        r = d["repeat"]
        targets.setdefault((r["sec"], r["block"]), []).append(r["text"])
    for (si, bi), sents in targets.items():
        blk = ms.sections[si].blocks[bi]
        text = blk.text
        for s in sents:
            if s in text:
                text = text.replace(s, "", 1)
                saved += len(s.split())
        blk.text = re.sub(r"\s{2,}", " ", text).strip()
    return saved


def long_windedness(ms: Manuscript) -> list[dict]:
    """Filler phrases that can be cut with no loss of meaning."""
    PATTERNS = [
        (r"\bit is important to note that\b", ""),
        (r"\bit should be noted that\b", ""),
        (r"\bit is worth noting that\b", ""),
        (r"\bin order to\b", "to"),
        (r"\bdue to the fact that\b", "because"),
        (r"\bin spite of the fact that\b", "although"),
        (r"\bfor the purpose of\b", "for"),
        (r"\ba large number of\b", "many"),
        (r"\bat this point in time\b", "now"),
        (r"\bin the event that\b", "if"),
        (r"\bwith regard to\b", "regarding"),
        (r"\bhas the ability to\b", "can"),
        (r"\bis able to\b", "can"),
        (r"\bin this research work\b", "in this work"),
    ]
    body = " ".join(b.text for s in ms.sections for b in s.blocks
                    if b.kind == "para")
    found = []
    for pat, rep in PATTERNS:
        hits = re.findall(pat, body, re.I)
        if hits:
            saved = len(hits) * max(len(pat.split()) - len(rep.split()), 1)
            found.append({"pattern": pat, "replacement": rep,
                          "count": len(hits), "approx_words": saved})
    return sorted(found, key=lambda x: -x["approx_words"])


def apply_long_windedness(ms: Manuscript, items: list[dict]) -> int:
    saved = 0
    for sec in ms.sections:
        for b in sec.blocks:
            if b.kind != "para":
                continue
            before = len(b.text.split())
            for it in items:
                b.text = re.sub(it["pattern"], it["replacement"], b.text,
                                flags=re.I)
            b.text = re.sub(r"\s{2,}", " ", b.text).strip()
            # a phrase cut at a sentence start leaves a lowercase opener
            b.text = re.sub(r"(^|(?<=[.!?]) )([a-z])",
                            lambda m: m.group(1) + m.group(2).upper(), b.text)
            saved += before - len(b.text.split())
    return saved
