"""Pre-submission compliance checks against a journal profile."""
from __future__ import annotations

import re
from .model import Manuscript


def run(ms: Manuscript, prof: dict) -> list[tuple[str, str]]:
    """Return list of (level, message); level in {ok, warn, fail}."""
    out: list[tuple[str, str]] = []

    ab_cfg = prof.get("abstract", {})
    if ab_cfg.get("type") == "structured":
        if not ms.structured_abstract:
            out.append(("fail", f"{prof['name']} requires a structured abstract "
                                f"({', '.join(ab_cfg.get('sections', []))}). "
                                "None detected — add it before submitting."))
        else:
            missing = [s for s in ab_cfg.get("sections", [])
                       if s not in ms.structured_abstract
                       and s not in ("Social implications",)]
            if missing:
                out.append(("warn", "Structured abstract missing: " + ", ".join(missing)))
    else:
        n = len(ms.abstract.split())
        cap = ab_cfg.get("max_words", 250)
        if n == 0:
            out.append(("fail", "No abstract detected."))
        elif n > cap:
            out.append(("fail", f"Abstract is {n} words; limit is {cap}."))
        else:
            out.append(("ok", f"Abstract {n}/{cap} words."))

    kw = prof.get("keywords", {})
    n = len(ms.keywords)
    if n < kw.get("min", 3):
        out.append(("warn", f"Only {n} keywords; journal expects {kw.get('min')}–{kw.get('max')}."))
    elif n > kw.get("max", 8):
        out.append(("warn", f"{n} keywords; maximum is {kw.get('max')}."))
    else:
        out.append(("ok", f"{n} keywords."))

    if not ms.authors:
        out.append(("fail", "No authors detected — check the front matter of the source file."))
    elif not any(a.email for a in ms.authors):
        out.append(("warn", "No corresponding-author email detected."))

    if not ms.references:
        out.append(("fail", "No reference list detected."))
    else:
        no_year = sum(1 for r in ms.references if not r.year)
        no_doi = sum(1 for r in ms.references if not r.doi)
        out.append(("ok", f"{len(ms.references)} references parsed."))
        if no_year:
            out.append(("warn", f"{no_year} references have no detectable year."))
        if no_doi > len(ms.references) * 0.5:
            out.append(("warn", f"{no_doi} references have no DOI — most publishers now expect them."))

    body = " ".join(b.text for s in ms.sections for b in s.blocks if b.kind == "para")
    cited = set(int(x) for x in re.findall(r"\[(\d{1,3})\]", body))
    if cited and ms.references:
        missing = sorted(c for c in cited if c > len(ms.references))
        uncited = sorted(set(range(1, len(ms.references) + 1)) - cited)
        if missing:
            out.append(("fail", f"In-text citations point past the reference list: {missing}"))
        if uncited:
            out.append(("warn", f"References never cited in text: {uncited[:15]}"))

    no_img = [f.label for f in ms.figures if not f.path]
    if no_img:
        out.append(("fail",
                    f"{len(no_img)} of {len(ms.figures)} figures have a caption but no image "
                    f"in the source file ({', '.join(no_img[:8])}"
                    f"{'…' if len(no_img) > 8 else ''}). They are kept as placeholders — "
                    "ask the author for the missing artwork."))
    # a caption that reads like a body sentence means a mention was mistaken
    # for a caption ("compares ...", "shows ...")
    verbish = [x.label for x in list(ms.figures) + list(ms.tables)
               if x.caption and re.match(r"^(shows?|presents?|displays?|compares?|"
                                          r"illustrates?|summari[sz]es?|lists?|depicts?|"
                                          r"reports?|gives?|provides?)\b", x.caption)]
    if verbish:
        out.append(("warn", "These captions start like a sentence about the object "
                            f"rather than a title — check them: {', '.join(verbish[:10])}"))
    no_cap = [f.label for f in ms.figures if not f.caption]
    if no_cap:
        out.append(("warn", f"Figures with no caption: {', '.join(no_cap[:10])}"
                            f"{'…' if len(no_cap) > 10 else ''}"))
    if ms.figures:
        out.append(("ok", f"{len(ms.figures)} figure(s) extracted."))
    for t in ms.tables:
        if not t.rows:
            out.append(("warn", f"Table '{t.label}' has a caption but no parsed cells."))

    for t in ms.tables:
        if len(t.rows) < 3:
            continue
        ncol = max(len(r) for r in t.rows)
        empty = [ci for ci in range(ncol)
                 if all(not (r[ci] if ci < len(r) else "").strip() for r in t.rows[1:])]
        if empty:
            from . import inline_math
            heads = [inline_math.to_plain(t.rows[0][ci] if ci < len(t.rows[0]) else "")
                     or f"col {ci+1}"
                     for ci in empty]
            out.append(("fail",
                        f"Table '{t.label}': column(s) {', '.join(h[:18] for h in heads[:6])} "
                        "have a header but every data cell is empty. The values are "
                        "missing from the source file itself — restore them before "
                        "formatting, or the output table will be blank."))

    tab_no_cap = [t.label for t in ms.tables if not t.caption]
    if tab_no_cap:
        out.append(("warn", f"Tables with no caption: {', '.join(tab_no_cap[:10])}"
                            f"{'…' if len(tab_no_cap) > 10 else ''}"))
    if ms.tables:
        out.append(("ok", f"{len(ms.tables)} table(s) extracted."))

    body_l = body.lower()
    for f in ms.figures:
        n = "".join(c for c in f.label if c.isdigit())
        if n and not re.search(rf"fig(?:ure)?\.?\s*{n}\b", body_l):
            out.append(("warn", f"Figure {n} is never referred to in the text."))
    for t in ms.tables:
        n = "".join(c for c in t.label if c.isdigit())
        if n and not re.search(rf"table\s*{n}\b", body_l):
            out.append(("warn", f"Table {n} is never referred to in the text."))

    if ms.equations:
        out.append(("ok", f"{len(ms.equations)} display equation(s) converted from "
                          "Word math."))
        inline_n = sum(len(getattr(b, "inline_eqs", None) or [])
                       for s in ms.sections for b in s.blocks)
        if inline_n:
            out.append(("ok", f"{inline_n} inline equation(s) preserved in the text."))
    img_only = [f.label for f in ms.figures
                if f.path and not f.caption]
    if len(img_only) >= 3:
        out.append(("warn",
                    f"{len(img_only)} images have no caption. If any of these are "
                    "equations pasted as pictures, they are being numbered as "
                    "figures — check before submitting."))

    heads = [s.title.lower() for s in ms.sections if s.level == 1]
    if not any("conclusion" in h for h in heads):
        out.append(("warn", "No Conclusion section detected."))
    if not any(("introduction" in h) for h in heads):
        out.append(("warn", "No Introduction section detected."))

    return out
