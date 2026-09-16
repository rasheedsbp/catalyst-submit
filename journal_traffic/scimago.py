"""Real quartiles from the SCImago Journal Rank CSV.

Why this exists
---------------
Elsevier's `ext_list` workbook — the Scopus source list — carries no quartile
or CiteScore column. Without another source, the tool had to fall back to a
citation-percentile proxy that ranked journals against whatever batch your
search returned. That is not a Scopus quartile and could be badly wrong: a
journal can be proxy-Q1 in a weak batch and genuinely Q3 in its field.

SCImago publishes the real thing, free, at scimagojr.com/journalrank.php
("Download data"). ~32,000 journals with SJR values and quartiles.

Two things about the file that break naive parsing:

  1. It is semicolon-delimited with EUROPEAN DECIMALS — the SJR of the top
     journal reads "104,065", not "104.065". Reading it with default settings
     silently produces nonsense numbers.
  2. `Issn` holds several ISSNs per row, comma-separated and without hyphens
     ("15424863, 00079235"), so each must be indexed separately.

Per-category quartiles
----------------------
`SJR Best Quartile` is the journal's best across all its subject categories,
which flatters it. The `Categories` column carries the real detail —
"Hematology (Q1); Oncology (Q3)" — and a journal's quartile genuinely differs
by field. Both are stored, so a search in a known subject can use the quartile
for THAT subject rather than the journal's most flattering one.

Note this is an SJR quartile, not a CiteScore quartile. They are related but
not identical, and a journal can sit in different quartiles under each. Say
"Q1 (SJR)" to a client rather than implying Scopus's own CiteScore ranking.
"""

from __future__ import annotations

import pickle
import re
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")

CATEGORY_RE = re.compile(r"^(.*?)\s*\((Q[1-4])\)\s*$")


def _norm_issn(v) -> str:
    """8 characters, no hyphens, leading zeros restored."""
    if v is None:
        return ""
    s = str(v).strip().upper()
    if not s or s in ("NAN", "NONE", "-"):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    s = s.replace("-", "").replace(" ", "")
    return s.zfill(8) if len(s) <= 8 else s


def _parse_quartile(v) -> int | None:
    s = str(v).strip().upper()
    for q in (1, 2, 3, 4):
        if s == f"Q{q}":
            return q
    return None


def _parse_categories(raw) -> dict[str, int]:
    """'Hematology (Q1); Oncology (Q3)' -> {'hematology': 1, 'oncology': 3}."""
    if not raw or pd.isna(raw):
        return {}
    out: dict[str, int] = {}
    for part in str(raw).split(";"):
        m = CATEGORY_RE.match(part.strip())
        if m:
            name, q = m.group(1).strip().lower(), _parse_quartile(m.group(2))
            if name and q:
                out[name] = q
    return out


def _to_float(v) -> float | None:
    """European decimal comma -> float."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().replace(".", "").replace(",", ".") if "," in str(v) else str(v)
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _cat_words(text: str) -> set[str]:
    """Content words of a category or subject name, for overlap matching."""
    skip = {"and", "the", "of", "for", "general", "miscellaneous", "all",
            "applied", "theoretical", "other"}
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in skip}


def _best_category(subject: str | None, cats: dict[str, int]):
    """The journal category that best matches the search subject.

    Exact containment first, then the category sharing the most content
    words. A single shared word is accepted only when the category has one
    content word to share — otherwise "networks" would match "Computer
    Networks and Communications" and "Social Networks" equally.
    """
    if not subject or not cats:
        return None

    sub = subject.lower()
    for name, q in cats.items():
        # Containment only counts for a phrase, and only on word boundaries.
        # A bare "networks" is inside "Computer Networks and Communications"
        # as a substring, which is not evidence that the paper belongs there.
        if name == sub:
            return name, q
        if len(sub.split()) > 1 and re.search(rf"\b{re.escape(sub)}\b", name):
            return name, q
        if len(name.split()) > 1 and re.search(rf"\b{re.escape(name)}\b", sub):
            return name, q

    sub_words = _cat_words(sub)
    # A single-word subject is too coarse to pick between a journal's
    # categories; fall back to the median rather than guessing one.
    if len(sub_words) < 2:
        return None

    best, best_score = None, 0.0
    for name, q in cats.items():
        words = _cat_words(name)
        if not words:
            continue
        shared = sub_words & words
        if not shared:
            continue
        # Both directions matter: the shared words must cover most of the
        # category AND be more than a single word of the subject, or a
        # two-word category like "Social Networks" is matched by the bare
        # word "networks".
        if len(shared) < 2 and len(sub_words) > 1:
            continue
        score = len(shared) / len(words)
        if score > best_score:
            best, best_score = (name, q), score

    return best if best_score >= 0.5 else None


class ScimagoIndex:
    """ISSN -> SJR quartile, SJR value, H-index, and per-category quartiles."""

    def __init__(self, data_dir: Path = DATA_DIR, journals_only: bool = True):
        self.by_issn: dict[str, dict] = {}
        self.data_dir = data_dir
        self.journals_only = journals_only
        self.source_file: Path | None = None
        self._load()

    # ------------------------------------------------------------ loading

    def _find_file(self) -> Path | None:
        if not self.data_dir.exists():
            return None
        candidates = [
            f for f in sorted(self.data_dir.iterdir())
            if f.is_file() and f.suffix.lower() == ".csv"
            and ("scimago" in f.name.lower() or "sjr" in f.name.lower())
        ]
        if candidates:
            return candidates[0]
        # Fall back to any CSV that has the tell-tale column.
        for f in sorted(self.data_dir.glob("*.csv")):
            try:
                head = f.open(encoding="utf-8", errors="ignore").readline()
            except OSError:
                continue
            if "SJR Best Quartile" in head:
                return f
        return None

    def _load(self) -> None:
        path = self._find_file()
        if path is None:
            return
        self.source_file = path

        cache = path.with_suffix(".parsed.pkl")
        if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
            try:
                with cache.open("rb") as fh:
                    self.by_issn = pickle.load(fh)
                return
            except Exception:  # noqa: BLE001
                pass

        try:
            # Semicolon-delimited; decimals are handled per-field because
            # pandas' decimal= applies to all numeric parsing and the file
            # mixes formats.
            df = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not read {path.name}: {exc}")
            return

        if "SJR Best Quartile" not in df.columns:
            print(f"  ! {path.name} has no 'SJR Best Quartile' column; skipped")
            return

        for _, row in df.iterrows():
            if self.journals_only:
                t = str(row.get("Type", "")).strip().lower()
                if t and t != "journal":
                    continue

            entry = {
                "sjr_title": row.get("Title"),
                "sjr_quartile": _parse_quartile(row.get("SJR Best Quartile")),
                "sjr_value": _to_float(row.get("SJR")),
                "h_index_sjr": _to_float(row.get("H index")),
                "sjr_categories": _parse_categories(row.get("Categories")),
                "sjr_areas": row.get("Areas"),
                "sjr_country": row.get("Country"),
                "sjr_open_access": str(row.get("Open Access", "")).strip() == "Yes",
            }

            for part in str(row.get("Issn", "")).split(","):
                key = _norm_issn(part)
                if key:
                    self.by_issn[key] = entry

        try:
            with cache.open("wb") as fh:
                pickle.dump(self.by_issn, fh)
        except OSError:
            pass

    # ------------------------------------------------------------- lookup

    def lookup(self, issns: list[str], subject: str | None = None) -> dict:
        """Quartile for these ISSNs, preferring the subject's own category.

        `SJR Best Quartile` is the journal's best across all its categories,
        which flatters journals that sit in one strong field and several weak
        ones. When a subject is supplied and matches a category, that
        category's quartile is used instead.
        """
        for issn in issns:
            key = _norm_issn(issn)
            if key not in self.by_issn:
                continue

            e = dict(self.by_issn[key])
            e["quartile"] = e.get("sjr_quartile")
            e["quartile_source"] = "sjr_best"

            cats = e.get("sjr_categories") or {}
            if cats:
                match = _best_category(subject, cats)
                if match:
                    e["quartile"] = match[1]
                    e["quartile_source"] = f"sjr_category:{match[0]}"
                else:
                    # No category matches the search subject, so the "best
                    # quartile" would be the journal's rank in some other
                    # field entirely — a journal that is Q1 in Oncology and
                    # Q4 in Engineering is not a Q1 option for an engineering
                    # paper. Reporting the best across all fields made almost
                    # every journal look like Q1, which is why filtering to
                    # Q2-Q4 returned nothing. The median across its
                    # categories is the honest summary when we cannot tell
                    # which field applies.
                    qs = sorted(cats.values())
                    e["quartile"] = qs[len(qs) // 2]
                    e["quartile_source"] = (
                        "sjr_best" if len(qs) == 1 else "sjr_median"
                    )

            return e

        return {}

    def summary(self) -> str:
        if not self.by_issn:
            return (
                "No SCImago file in data/ — quartiles will be citation-"
                "percentile proxies. Download the CSV from "
                "scimagojr.com/journalrank.php"
            )
        quarts = [
            e["sjr_quartile"] for e in self.by_issn.values()
            if e.get("sjr_quartile")
        ]
        name = self.source_file.name if self.source_file else "?"
        dist = {f"Q{q}": quarts.count(q) for q in (1, 2, 3, 4)}
        return (
            f"SCImago quartiles loaded from {name}: {len(self.by_issn):,} ISSNs "
            f"({dist})"
        )
