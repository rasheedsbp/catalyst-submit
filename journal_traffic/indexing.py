"""Which lists a journal appears on: ABDC and PubMed.

Scopus and SCImago already have their own modules. This one adds the two
further lists clients ask about by name — "is it ABDC A?", "is it in
PubMed?" — behind a single registry so the pipeline annotates every journal
once and the UI can filter on either or both.

Matching
--------
ISSN first, always. Print and electronic ISSNs are both indexed, normalised
to eight characters without a hyphen, because every source formats them
differently and Excel eats leading zeros.

Title matching is the fallback, and only for ABDC, whose spreadsheet has no
usable ISSN column in some sheets. It is deliberately strict — casefolded,
punctuation stripped, leading article removed — because fuzzy title matching
across 30,000 journals produces confident nonsense ("Journal of Marketing"
vs "Journal of Marketing Research").

Nothing here guesses. A journal that is not found is reported as not found,
never as "probably indexed".
"""

from __future__ import annotations

import pickle
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")

ABDC_RANKS = ["A*", "A", "B", "C"]


def norm_issn(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().upper()
    if not s or s in ("NAN", "NONE", "-", "N/A"):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[^0-9X]", "", s)
    if not s:
        return ""
    return s.zfill(8) if len(s) <= 8 else s[:8]


def norm_title(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cache_path(src: Path, tag: str) -> Path:
    return src.parent / f".{tag}_{src.stem}.pkl"


def _load_cached(src: Path, tag: str):
    cache = _cache_path(src, tag)
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        try:
            with open(cache, "rb") as fh:
                return pickle.load(fh)
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_cached(src: Path, tag: str, obj) -> None:
    try:
        with open(_cache_path(src, tag), "wb") as fh:
            pickle.dump(obj, fh)
    except Exception:  # noqa: BLE001
        pass


def _read_table(path: Path, **kw) -> list[pd.DataFrame]:
    """Read a csv/xls/xlsx into one frame per sheet."""
    if path.suffix.lower() == ".csv":
        for sep in (",", ";", "\t"):
            try:
                df = pd.read_csv(path, sep=sep, dtype=str, **kw)
                if df.shape[1] > 1:
                    return [df]
            except Exception:  # noqa: BLE001
                continue
        return []
    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str, **kw)
    except Exception:  # noqa: BLE001
        return []
    return list(sheets.values())


def _pick_column(df: pd.DataFrame, *patterns: str) -> str | None:
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for col in df.columns:
            if rx.search(str(col)):
                return col
    return None


def _repair_header(df: pd.DataFrame, probe: str) -> pd.DataFrame:
    """Promote the real header row when a banner occupies row 1.

    ABDC and several Clarivate exports put a disclaimer above the table, so
    pandas reads 'Unnamed: 0' columns and the true header sits in the data.
    """
    if _pick_column(df, probe):
        return df
    rx = re.compile(probe, re.I)
    for i in range(min(8, len(df))):
        row = df.iloc[i]
        if any(rx.search(str(v)) for v in row.values if pd.notna(v)):
            out = df.iloc[i + 1:].copy()
            out.columns = [str(v).strip() for v in row.values]
            return out.reset_index(drop=True)
    return df


# ----------------------------------------------------------------- ABDC

@dataclass
class AbdcIndex:
    """ABDC Journal Quality List ratings (A*, A, B, C)."""

    data_dir: Path = DATA_DIR
    by_issn: dict[str, dict] = field(default_factory=dict)
    by_title: dict[str, dict] = field(default_factory=dict)
    source: Path | None = None
    year: str | None = None

    def __post_init__(self) -> None:
        files = [f for f in sorted(self.data_dir.glob("*"))
                 if f.is_file() and "abdc" in f.name.lower()
                 and f.suffix.lower() in (".csv", ".xls", ".xlsx")]
        if not files:
            return
        self.source = files[-1]
        cached = _load_cached(self.source, "abdc")
        if cached:
            self.by_issn, self.by_title, self.year = cached
            return
        self._parse(self.source)
        _save_cached(self.source, "abdc",
                     (self.by_issn, self.by_title, self.year))

    def _parse(self, path: Path) -> None:
        # The workbook holds one sheet per review year; the newest wins.
        best_year, best = None, None
        for raw in _read_table(path):
            df = _repair_header(raw, r"rating|rank|issn|title")
            c_rating = _pick_column(df, r"(20\d\d).*(rating|rank)",
                                    r"(rating|rank).*(20\d\d)", r"^rating$",
                                    r"\brating\b", r"\brank\b")
            c_title = _pick_column(df, r"journal\s*title", r"^title$",
                                   r"journal\s*name")
            if not (c_rating and c_title):
                continue
            m = re.search(r"(20\d\d)", str(c_rating))
            year = m.group(1) if m else "unknown"
            if best_year is None or year > best_year:
                best_year, best = year, (df, c_rating, c_title)

        if not best:
            return
        df, c_rating, c_title = best
        self.year = best_year
        c_issn = _pick_column(df, r"^issn", r"print\s*issn")
        c_eissn = _pick_column(df, r"issn.*online", r"e-?issn", r"online\s*issn")
        c_for = _pick_column(df, r"\bfor\b|field of research")
        c_pub = _pick_column(df, r"publisher")

        for _, row in df.iterrows():
            rating = str(row.get(c_rating) or "").strip().upper().replace("*", "*")
            if rating not in ABDC_RANKS:
                continue
            title = row.get(c_title)
            if not title or str(title).strip().lower() == "nan":
                continue
            rec = {
                "in_abdc": True,
                "abdc_rank": rating,
                "abdc_year": self.year,
                "abdc_for": (str(row.get(c_for)).strip()
                             if c_for and pd.notna(row.get(c_for)) else None),
                "abdc_publisher": (str(row.get(c_pub)).strip()
                                   if c_pub and pd.notna(row.get(c_pub)) else None),
            }
            for col in (c_issn, c_eissn):
                if col:
                    key = norm_issn(row.get(col))
                    if key:
                        self.by_issn[key] = rec
            self.by_title.setdefault(norm_title(title), rec)

    @property
    def available(self) -> bool:
        return bool(self.by_title)

    def lookup(self, issns, title: str | None = None) -> dict:
        for i in issns or []:
            rec = self.by_issn.get(norm_issn(i))
            if rec:
                return rec
        if title:
            rec = self.by_title.get(norm_title(title))
            if rec:
                return rec
        return {"in_abdc": False}

    def summary(self) -> str:
        if not self.available:
            return "ABDC: list not downloaded — ABDC filter unavailable."
        return (f"ABDC {self.year or ''}: {len(self.by_title):,} rated journals "
                f"({self.source.name})")


# --------------------------------------------------------------- PubMed

@dataclass
class PubMedIndex:
    """PubMed / MEDLINE serials, parsed from NLM's J_Medline.txt."""

    data_dir: Path = DATA_DIR
    by_issn: dict[str, dict] = field(default_factory=dict)
    by_title: dict[str, dict] = field(default_factory=dict)
    source: Path | None = None

    def __post_init__(self) -> None:
        for name in ("J_Medline.txt", "J_Entrez.txt"):
            path = self.data_dir / name
            if path.exists():
                self.source = path
                break
        if not self.source:
            return
        cached = _load_cached(self.source, "pubmed")
        if cached:
            self.by_issn, self.by_title = cached
            return
        self._parse(self.source)
        _save_cached(self.source, "pubmed", (self.by_issn, self.by_title))

    def _parse(self, path: Path) -> None:
        rec: dict[str, str] = {}

        def flush():
            if not rec.get("JournalTitle"):
                return
            entry = {
                "in_pubmed": True,
                "pubmed_title": rec["JournalTitle"],
                "medline_abbr": rec.get("MedAbbr") or None,
                "nlm_id": rec.get("NlmId") or None,
            }
            for key in ("ISSN (Print)", "ISSN (Online)"):
                i = norm_issn(rec.get(key))
                if i:
                    self.by_issn[i] = entry
            self.by_title.setdefault(norm_title(rec["JournalTitle"]), entry)

        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("---"):
                    flush()
                    rec = {}
                    continue
                if ":" in line:
                    k, _, v = line.partition(":")
                    rec[k.strip()] = v.strip()
        flush()

    @property
    def available(self) -> bool:
        return bool(self.by_issn)

    def lookup(self, issns, title: str | None = None) -> dict:
        for i in issns or []:
            rec = self.by_issn.get(norm_issn(i))
            if rec:
                return rec
        if title:
            rec = self.by_title.get(norm_title(title))
            if rec:
                return rec
        return {"in_pubmed": False}

    def summary(self) -> str:
        if not self.available:
            return "PubMed: serials file not downloaded — PubMed filter unavailable."
        return f"PubMed: {len(self.by_issn):,} ISSNs ({self.source.name})"


# -------------------------------------------------------------- registry

class IndexRegistry:
    """All three lists behind one call, so the pipeline annotates once."""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.abdc = AbdcIndex(data_dir)
        self.pubmed = PubMedIndex(data_dir)

    def annotate(self, issns, title: str | None = None) -> dict:
        out: dict = {}
        out.update(self.abdc.lookup(issns, title))
        out.update(self.pubmed.lookup(issns, title))
        out.setdefault("abdc_rank", None)
        return out

    def availability(self) -> dict[str, bool]:
        return {
            "abdc": self.abdc.available,
            "pubmed": self.pubmed.available,
        }

    def summaries(self) -> list[str]:
        return [self.abdc.summary(), self.pubmed.summary()]


def apply_index_filters(
    df: pd.DataFrame,
    require_pubmed: bool = False,
    abdc_ranks: list[str] | None = None,
) -> pd.DataFrame:
    """Keep only rows satisfying every requested list membership."""
    if df.empty:
        return df
    out = df
    if require_pubmed and "in_pubmed" in out.columns:
        out = out[out["in_pubmed"].fillna(False).astype(bool)]
    if abdc_ranks and "abdc_rank" in out.columns:
        out = out[out["abdc_rank"].isin(abdc_ranks)]
    return out
