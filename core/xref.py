"""Insert in-text mentions for figures, tables and equations that have none.

Journals require every float to be referred to in the text ("as shown in Table
5"), and reviewers routinely reject papers where they are not. Client
manuscripts frequently drop these when content is moved around.

What this does: builds the mention out of the object's **own caption** and puts
it at the end of the paragraph immediately before the object. Nothing is
invented — if the caption says "Statistics of attack happened in gas pipeline",
the sentence says exactly that, with no claim about what the data shows.

What this deliberately does not do: describe the contents of a table or figure.
Writing "Table 5 shows that accuracy improves with depth" means reading the data
and making a claim on the author's behalf. That claim would carry their name
into peer review, and a formatter has no business manufacturing it.
"""
from __future__ import annotations

import re

from .model import Manuscript, Block


def _cited(ms: Manuscript) -> tuple[set[int], set[int], set[int]]:
    body = " ".join(b.text for s in ms.sections for b in s.blocks
                    if b.kind == "para").lower()
    figs = {int(n) for n in re.findall(r"\bfig(?:ure)?s?\.?\s*(\d{1,3})", body)}
    tabs = {int(n) for n in re.findall(r"\btables?\s*(\d{1,3})", body)}
    # roman table numbers, common in IEEE
    roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
             "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
             "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18}
    for m in re.findall(r"\btable\s+([ivx]{1,6})\b", body):
        if m in roman:
            tabs.add(roman[m])
    eqs = {int(n) for n in re.findall(r"\beq(?:uation)?s?\.?\s*\(?(\d{1,3})\)?",
                                      body)}
    eqs |= {int(n) for n in re.findall(r"\((\d{1,3})\)", body)}
    return figs, tabs, eqs


def _num(label: str) -> int:
    return int(re.sub(r"\D", "", label) or 0)


def _sentence(kind: str, num: int, caption: str) -> str:
    """A neutral mention built only from the caption."""
    from . import inline_math
    cap = inline_math.to_plain(caption or "").strip().rstrip(".")
    name = {"figure": "Fig.", "table": "Table", "equation": "Eq."}[kind]
    if not cap:
        return f"{name} {num} is presented below."
    # lowercase a leading capital unless it looks like a proper noun/acronym
    if len(cap) > 1 and cap[0].isupper() and not cap[1].isupper():
        cap = cap[0].lower() + cap[1:]
    verb = "illustrates" if kind == "figure" else "presents"
    if kind == "equation":
        return f"{name} ({num}) is given below."
    return f"{name} {num} {verb} the {cap}." if not cap.startswith(("the ", "a ")) \
        else f"{name} {num} {verb} {cap}."


def find_missing(ms: Manuscript) -> list[dict]:
    """Every float with no in-text mention, with the sentence that would be added."""
    fig_c, tab_c, eq_c = _cited(ms)
    caps = {}
    for f in ms.figures:
        caps[f.label] = ("figure", _num(f.label), f.caption)
    for t in ms.tables:
        caps[t.label] = ("table", _num(t.label), t.caption)
    for e in ms.equations:
        caps[e.label] = ("equation", int(e.number or _num(e.label)), "")

    cited = {"figure": fig_c, "table": tab_c, "equation": eq_c}
    out = []
    for si, sec in enumerate(ms.sections):
        for bi, b in enumerate(sec.blocks):
            if b.kind not in ("figure", "table", "equation") or b.ref not in caps:
                continue
            kind, num, cap = caps[b.ref]
            if num in cited[kind]:
                continue
            # the paragraph this mention should be appended to
            target = None
            for k in range(bi - 1, -1, -1):
                if sec.blocks[k].kind == "para" and sec.blocks[k].text.strip():
                    target = k
                    break
            out.append({
                "label": b.ref, "kind": kind, "number": num, "caption": cap,
                "section": sec.title, "sec_index": si, "block_index": bi,
                "target_block": target,
                "sentence": _sentence(kind, num, cap),
                "after": (sec.blocks[target].text[-90:] if target is not None
                          else "(start of section — will be placed before the object)"),
            })
    return out


def apply(ms: Manuscript, items: list[dict], accepted: set[str]) -> list[str]:
    """Insert the approved mentions. Returns descriptions of what was added."""
    done = []
    # work backwards so inserted blocks don't shift later indices
    for it in sorted(items, key=lambda x: (-x["sec_index"], -x["block_index"])):
        if it["label"] not in accepted:
            continue
        sec = ms.sections[it["sec_index"]]
        tb = it["target_block"]
        if tb is not None and tb < len(sec.blocks) and sec.blocks[tb].kind == "para":
            txt = sec.blocks[tb].text.rstrip()
            if not txt.endswith((".", "?", "!", ":")):
                txt += "."
            sec.blocks[tb].text = f"{txt} {it['sentence']}"
        else:
            sec.blocks.insert(it["block_index"], Block(kind="para",
                                                       text=it["sentence"]))
        done.append(f"{it['kind'].title()} {it['number']}")
    return done
