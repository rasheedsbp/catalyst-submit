"""Scopus coverage status, read from Elsevier's official `ext_list` workbook.

Written against the real file (ext_list_Jul_2026.xlsx), not a guess at it.
Structure that matters:

  Sheet "Scopus Sources <Month> <Year>" — 48,888 rows, 52 columns
      ISSN / EISSN           8-digit, NO hyphens, and Excel strips leading
                             zeros, so "04851439" arrives as an integer
      Active or Inactive     32,050 Active / 16,838 Inactive
      Titles Discontinued    967 rows flagged "Discontinued by Scopus"
      Source Type            Journal / Book Series / Trade Journal
      No CiteScore or quartile columns at all — this workbook does not
      carry them. Quartiles must come from elsewhere.

  Sheet "Discontinued Titles <Month> <Year>" — real header on ROW 2, with a
      disclaimer banner occupying row 1.

Three states are distinguished, because they mean different things:

  active        indexed now, safe to submit
  INACTIVE      was indexed, coverage has stopped — a paper published here
                now will not be a Scopus publication
  DISCONTINUED  explicitly removed by Scopus

Both INACTIVE and DISCONTINUED are disqualifying for a client submission.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import pandas as pd

DATA_DIR = Path("data")

# Empirical acceptance-rate priors by quartile. Wide bands on purpose — used
# only when nothing better exists, and the output flags them as such.
QUARTILE_PRIORS = {
    1: (0.08, 0.22),
    2: (0.18, 0.36),
    3: (0.32, 0.52),
    4: (0.45, 0.70),
}

FIELD_ADJUSTMENT = {
    "computer science": 0.85,
    "engineering": 0.90,
    "mathematics": 0.95,
    "medicine": 1.00,
    "management": 1.15,
    "business": 1.15,
    "social sciences": 1.10,
}


def _norm_issn(v) -> str:
    """Normalise to 8 characters, no hyphens, leading zeros restored.

    Excel reads ISSN columns as numbers, so 04851439 arrives as the integer
    4851439 and zero-padding is mandatory. Floats appear too when a column
    contains blanks.
    """
    if v is None:
        return ""
    s = str(v).strip().upper()
    if not s or s in ("NAN", "NONE"):
        return ""
    if s.endswith(".0"):
        s = s[:-2]
    s = s.replace("-", "").replace(" ", "")
    return s.zfill(8) if len(s) <= 8 else s


class ScopusIndex:
    """Lookup of ISSN -> coverage status, built from the ext_list workbook."""

    def __init__(self, data_dir: Path = DATA_DIR, journals_only: bool = True):
        self.active: dict[str, dict] = {}
        self.inactive: dict[str, dict] = {}
        self.discontinued: dict[str, dict] = {}
        self.data_dir = data_dir
        self.journals_only = journals_only
        self.source_file: Path | None = None
        self._load()

    # ------------------------------------------------------------- loading

    def _load(self) -> None:
        if not self.data_dir.exists():
            return

        candidates = [
            f for f in sorted(self.data_dir.iterdir())
            if f.is_file() and f.suffix.lower() in (".xlsx", ".xlsm", ".xls")
        ]
        if not candidates:
            return

        # Elsevier's serials workbook is named ext_list_<Month>_<Year>.xlsx.
        # The book list is a different file and must not be mistaken for it.
        def rank(f: Path) -> int:
            n = f.name.lower()
            if "book" in n:
                return 3                      # wrong file — last resort
            if "ext_list" in n:
                return 0
            if "source" in n or "title" in n:
                return 1
            return 2

        chosen = sorted(candidates, key=rank)[0]
        if "book" in chosen.name.lower():
            print(f"  ! {chosen.name} looks like the BOOK list, not the serial")
            print("    source list. Download 'Source title list' (ext_list_*.xlsx).")
            return

        self.source_file = chosen

        # Parsing 48k rows takes ~12s. Cache it against the file's mtime so
        # subsequent runs are instant and a new release invalidates itself.
        cache = chosen.with_suffix(".parsed.pkl")
        if cache.exists() and cache.stat().st_mtime >= chosen.stat().st_mtime:
            try:
                with cache.open("rb") as fh:
                    self.active, self.inactive, self.discontinued = pickle.load(fh)
                return
            except Exception:  # noqa: BLE001
                pass  # corrupt cache — reparse

        try:
            xl = pd.ExcelFile(chosen)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not open {chosen.name}: {exc}")
            return

        for sheet in xl.sheet_names:
            low = sheet.lower()
            if "asjc" in low or "conf" in low or "medline" in low:
                continue                       # reference sheets
            if "discontinued" in low:
                self._ingest_discontinued(xl, sheet)
            elif "source" in low:
                self._ingest_sources(xl, sheet)
            # "Accepted Titles" is skipped: those are not yet indexed.

        try:
            with cache.open("wb") as fh:
                pickle.dump(
                    (self.active, self.inactive, self.discontinued), fh
                )
        except OSError:
            pass  # cache is an optimisation, never load-bearing

    def _ingest_sources(self, xl: pd.ExcelFile, sheet: str) -> None:
        """Main sheet: split by Active/Inactive and the discontinued flag."""
        try:
            df = pd.read_excel(xl, sheet_name=sheet)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not read sheet '{sheet}': {exc}")
            return

        if "ISSN" not in df.columns and "EISSN" not in df.columns:
            print(f"  ! sheet '{sheet}' has no ISSN column; skipped")
            return

        asjc_col = "All Science Journal Classification Codes (ASJC)"

        for _, row in df.iterrows():
            if self.journals_only:
                stype = str(row.get("Source Type", "")).strip().lower()
                if stype and "journal" not in stype:
                    continue

            entry = {
                "title": row.get("Source Title"),
                "publisher": row.get("Publisher"),
                "asjc": row.get(asjc_col) if asjc_col in df.columns else None,
                "citescore": None,   # not present in this workbook
                "quartile": None,    # not present in this workbook
                "subject_area": None,
            }

            if pd.notna(row.get("Titles Discontinued by Scopus")):
                target = self.discontinued
            elif str(row.get("Active or Inactive", "")).strip().lower() == "inactive":
                target = self.inactive
            else:
                target = self.active

            for col in ("ISSN", "EISSN"):
                key = _norm_issn(row.get(col))
                if key:
                    target[key] = entry

    def _ingest_discontinued(self, xl: pd.ExcelFile, sheet: str) -> None:
        """Discontinued sheet: real header sits on row 2 under a banner."""
        try:
            df = pd.read_excel(xl, sheet_name=sheet, header=1)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not read sheet '{sheet}': {exc}")
            return

        cols = {str(c).strip().lower(): c for c in df.columns}
        issn_cols = [cols[k] for k in cols if k in ("issn", "eissn")]
        if not issn_cols:
            print(f"  ! sheet '{sheet}' header not found on row 2; skipped")
            return

        title_col = next((cols[k] for k in cols if "source title" in k), None)
        pub_col = next((cols[k] for k in cols if "publisher" in k), None)
        year_col = next((cols[k] for k in cols if k == "year"), None)

        for _, row in df.iterrows():
            entry = {
                "title": row.get(title_col) if title_col else None,
                "publisher": row.get(pub_col) if pub_col else None,
                "discontinued_year": row.get(year_col) if year_col else None,
                "citescore": None,
                "quartile": None,
                "subject_area": None,
            }
            for col in issn_cols:
                key = _norm_issn(row.get(col))
                if key:
                    self.discontinued[key] = entry
                    # Discontinued wins over any active entry for the same ISSN.
                    self.active.pop(key, None)
                    self.inactive.pop(key, None)

    # ------------------------------------------------------------- lookup

    def status(self, issns: list[str]) -> dict:
        keys = [k for k in (_norm_issn(i) for i in issns if i) if k]

        for k in keys:
            if k in self.discontinued:
                return {"scopus_status": "DISCONTINUED", **self.discontinued[k]}
        for k in keys:
            if k in self.inactive:
                return {"scopus_status": "INACTIVE", **self.inactive[k]}
        for k in keys:
            if k in self.active:
                return {"scopus_status": "active", **self.active[k]}
        return {
            "scopus_status": "unknown",
            "quartile": None,
            "citescore": None,
            "subject_area": None,
        }

    def summary(self) -> str:
        if not (self.active or self.inactive or self.discontinued):
            return "No Scopus data loaded."
        name = self.source_file.name if self.source_file else "?"
        return (
            f"Scopus list loaded from {name}: "
            f"{len(self.active):,} active, {len(self.inactive):,} inactive, "
            f"{len(self.discontinued):,} discontinued ISSNs"
        )


def parse_quartile(value) -> int | None:
    if value is None:
        return None
    s = str(value).upper().strip()
    for q in (1, 2, 3, 4):
        if s in {f"Q{q}", str(q), f"{q}.0"}:
            return q
    return None


def quartile_prior(quartile: int | None, subject: str | None) -> tuple[float, float]:
    """Fallback acceptance-rate band. Tier D evidence."""
    if quartile not in QUARTILE_PRIORS:
        return (0.10, 0.65)
    lo, hi = QUARTILE_PRIORS[quartile]
    if subject:
        s = subject.lower()
        for key, factor in FIELD_ADJUSTMENT.items():
            if key in s:
                lo, hi = lo * factor, min(hi * factor, 0.85)
                break
    return lo, hi
