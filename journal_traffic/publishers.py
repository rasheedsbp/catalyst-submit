"""Publisher normalisation and diversity enforcement.

The problem this solves
-----------------------
A real run returned 132 journals of which 125 were Inderscience — under three
different spellings ("Inderscience", "Inderscience Publishers", "Inderscience
Enterprises Ltd"). Two failures at once:

  1. OpenAlex records publisher names inconsistently, so any per-publisher cap
     applied to the raw string silently fails to group them.
  2. Nothing was capping publishers anyway, so one publisher with many small
     journals in a niche can swamp the entire result.

Both matter for a shortlist. A client wants options across publishers — one
publisher's editorial standards, APCs and turnaround are correlated across its
whole portfolio, so 125 of its journals is really one option, not 125.
"""

from __future__ import annotations

import re

import pandas as pd

# Canonical names for the publishers that appear most in engineering, CS and
# management. Matched as substrings against the lowercased raw name, longest
# key first so "taylor & francis" wins over "francis".
PUBLISHER_ALIASES = {
    "elsevier": "Elsevier",
    "sciencedirect": "Elsevier",
    "pergamon": "Elsevier",
    "academic press": "Elsevier",
    "cell press": "Elsevier",
    "springer": "Springer Nature",
    "nature portfolio": "Springer Nature",
    "nature publishing": "Springer Nature",
    "biomed central": "Springer Nature",
    "palgrave": "Springer Nature",
    "wiley": "Wiley",
    "blackwell": "Wiley",
    "hindawi": "Wiley",
    "institute of electrical and electronics engineers": "IEEE",
    "ieee": "IEEE",
    "association for computing machinery": "ACM",
    "acm": "ACM",
    "taylor & francis": "Taylor & Francis",
    "taylor and francis": "Taylor & Francis",
    "routledge": "Taylor & Francis",
    "informa": "Taylor & Francis",
    "mdpi": "MDPI",
    "multidisciplinary digital publishing": "MDPI",
    "sage": "SAGE",
    "emerald": "Emerald",
    "inderscience": "Inderscience",
    "frontiers media": "Frontiers",
    "frontiers": "Frontiers",
    "public library of science": "PLOS",
    "plos": "PLOS",
    "oxford university press": "Oxford UP",
    "cambridge university press": "Cambridge UP",
    "de gruyter": "De Gruyter",
    "iop publishing": "IOP",
    "institute of physics": "IOP",
    "american chemical society": "ACS",
    "royal society of chemistry": "RSC",
    "american institute of physics": "AIP",
    "asme": "ASME",
    "asce": "ASCE",
    "jmir": "JMIR",
    "bentham": "Bentham",
    "world scientific": "World Scientific",
    "techno-press": "Techno-Press",
    "river publishers": "River",
    "iaes": "IAES",
    "institute of advanced engineering and science": "IAES",
    "scientific research publishing": "SCIRP",
}

# Corporate suffixes that carry no information and break exact grouping.
SUFFIX_NOISE = re.compile(
    r"\b(inc|ltd|llc|limited|gmbh|bv|b\.v|sa|s\.a|ag|co|corp|corporation|"
    r"company|publishers?|publishing|press|group|enterprises?|media|"
    r"international|holdings?)\b\.?",
    re.I,
)


def normalize_publisher(raw) -> str:
    """Map a raw publisher string to a canonical name.

    'Inderscience Enterprises Ltd', 'Inderscience Publishers' and
    'Inderscience' all become 'Inderscience'.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "Unknown"
    text = str(raw).strip()
    if not text or text.lower() in ("nan", "none"):
        return "Unknown"

    low = text.lower()
    for alias in sorted(PUBLISHER_ALIASES, key=len, reverse=True):
        if alias in low:
            return PUBLISHER_ALIASES[alias]

    # Unknown publisher: strip corporate noise so variants still group.
    cleaned = SUFFIX_NOISE.sub("", text)
    cleaned = re.sub(r"[^\w\s&-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -&")
    return cleaned.title() if cleaned else "Unknown"


def add_publisher_column(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["publisher_group"] = out["publisher"].apply(normalize_publisher)
    return out


def enforce_diversity(
    df: pd.DataFrame,
    max_per_publisher: int = 3,
    score_col: str = "opportunity_score",
    verbose: bool = True,
) -> pd.DataFrame:
    """Keep at most N journals per publisher, best-scoring first.

    Rationale: one publisher's editorial standards, APCs and turnaround are
    correlated across its whole portfolio, so ten of its journals on a
    shortlist is really one option presented ten times. Capping forces the
    list to offer genuine alternatives.
    """
    if df.empty or "publisher_group" not in df.columns:
        return df

    sort_col = score_col if score_col in df.columns else None
    ordered = df.sort_values(sort_col, ascending=False) if sort_col else df

    kept = ordered.groupby("publisher_group", group_keys=False).head(
        max_per_publisher
    )

    if verbose:
        dropped = len(ordered) - len(kept)
        if dropped:
            counts = ordered["publisher_group"].value_counts()
            over = counts[counts > max_per_publisher]
            if len(over):
                detail = ", ".join(
                    f"{name} ({n} -> {max_per_publisher})"
                    for name, n in over.head(4).items()
                )
                print(
                    f"    publisher cap: dropped {dropped} journal(s) — {detail}"
                )

    return kept.sort_values(sort_col, ascending=False) if sort_col else kept


def diversity_report(df: pd.DataFrame, top: int = 8) -> str:
    """Human-readable concentration summary, with a warning when skewed."""
    if df.empty or "publisher_group" not in df.columns:
        return ""

    counts = df["publisher_group"].value_counts()
    total = len(df)
    lines = [f"Publishers: {len(counts)} distinct across {total} journals"]

    for name, n in counts.head(top).items():
        share = n / total
        bar = "#" * max(1, int(share * 30))
        lines.append(f"  {name[:28]:<28} {n:>4}  {share:>5.0%} {bar}")

    top_share = counts.iloc[0] / total if len(counts) else 0
    if top_share > 0.5:
        lines.append(
            f"\n  WARNING: {counts.index[0]} accounts for {top_share:.0%} of "
            "results. That usually means the search term matched a publisher's "
            "portfolio rather than a field. Try a different term, or raise "
            "--limit so other publishers surface."
        )
    return "\n".join(lines)
