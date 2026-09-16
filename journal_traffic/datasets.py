"""Fetching and caching the external journal lists the tool depends on.

Everything here is a public, free source. Nothing is scraped from behind a
paywall and nothing is bundled with the code — the files are downloaded on
first use into ./data/ and refreshed only when they age out, so a deployment
starts empty and fills itself.

  SCImago   scimagojr.com serves the full journal rank table as a CSV from a
            stable query URL. Free, no key.
  Scopus    Elsevier publishes ext_list_<Month>_<Year>.xlsx from the Scopus
            content page. The filename changes every few months, so the page
            is parsed for the current link rather than a URL being hardcoded.
  PubMed    NLM's own serials file, ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt.
            Authoritative for "is this journal in PubMed" and free.
  ABDC      The Journal Quality List spreadsheet, linked from the ABDC page.
            Same approach as Scopus: resolve the link, then download.
Every fetch returns a Result rather than raising, because the UI shows the
status of all four side by side and one dead host must not take the page down.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

DATA_DIR = Path("data")
UA = {"User-Agent": "journal-traffic-estimator/2.0 (research tooling)"}
TIMEOUT = 120

SCIMAGO_CSV = "https://www.scimagojr.com/journalrank.php?out=xls"
SCOPUS_PAGE = "https://www.elsevier.com/products/scopus/content"
PUBMED_SERIALS = "https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt"
ABDC_PAGE = "https://abdc.edu.au/abdc-journal-quality-list/"

# How long a cached copy stays acceptable. These lists change slowly; the
# Scopus one changes most often because titles get discontinued.
MAX_AGE_DAYS = {
    "scimago": 120,
    "scopus": 60,
    "pubmed": 60,
    "abdc": 365,
}


@dataclass
class Result:
    key: str
    ok: bool
    path: Path | None
    message: str


def _age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


def _fresh(path: Path, key: str) -> bool:
    return path.exists() and path.stat().st_size > 1024 and \
        _age_days(path) <= MAX_AGE_DAYS.get(key, 90)


def _download(url: str, dest: Path, key: str) -> Result:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, headers=UA, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
            if tmp.stat().st_size < 1024:
                tmp.unlink(missing_ok=True)
                return Result(key, False, None, "Download was empty.")
            tmp.replace(dest)
    except requests.RequestException as exc:
        return Result(key, False, None, f"{type(exc).__name__}: {exc}")
    size = dest.stat().st_size / 1e6
    return Result(key, True, dest, f"Downloaded {dest.name} ({size:.1f} MB).")


def _resolve_link(page_url: str, pattern: str) -> str | None:
    """Find the current download link on a publisher's page.

    Both Elsevier and ABDC rename their spreadsheet every release, so the
    link is looked up instead of hardcoded.
    """
    try:
        html = requests.get(page_url, headers=UA, timeout=60).text
    except requests.RequestException:
        return None

    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
    rx = re.compile(pattern, re.I)
    for href in hrefs:
        if rx.search(href):
            if href.startswith("//"):
                return "https:" + href
            if href.startswith("/"):
                root = "/".join(page_url.split("/")[:3])
                return root + href
            if href.startswith("http"):
                return href
    return None


# ------------------------------------------------------------- individual

def fetch_scimago(data_dir: Path = DATA_DIR, force: bool = False) -> Result:
    dest = data_dir / "scimagojr_latest.csv"
    if not force and _fresh(dest, "scimago"):
        return Result("scimago", True, dest, f"Cached ({_age_days(dest):.0f} d old).")
    existing = sorted(data_dir.glob("scimagojr_*.csv"))
    res = _download(SCIMAGO_CSV, dest, "scimago")
    if not res.ok and existing:
        return Result("scimago", True, existing[-1],
                      f"Download failed, using {existing[-1].name}.")
    return res


def fetch_scopus(data_dir: Path = DATA_DIR, force: bool = False) -> Result:
    existing = [f for f in data_dir.glob("ext_list*.xlsx")]
    if existing and not force and _fresh(existing[0], "scopus"):
        return Result("scopus", True, existing[0], f"Cached: {existing[0].name}.")

    link = _resolve_link(SCOPUS_PAGE, r"ext_list.*\.(xlsx|xlsb|xls)")
    if not link:
        if existing:
            return Result("scopus", True, existing[0],
                          f"Link not found on Elsevier's page; using {existing[0].name}.")
        return Result(
            "scopus", False, None,
            "Could not find the ext_list link on elsevier.com. Download "
            "'Source title list' manually into data/.",
        )
    name = link.split("/")[-1].split("?")[0]
    return _download(link, data_dir / name, "scopus")


def fetch_pubmed(data_dir: Path = DATA_DIR, force: bool = False) -> Result:
    dest = data_dir / "J_Medline.txt"
    if not force and _fresh(dest, "pubmed"):
        return Result("pubmed", True, dest, f"Cached ({_age_days(dest):.0f} d old).")
    return _download(PUBMED_SERIALS, dest, "pubmed")


def fetch_abdc(data_dir: Path = DATA_DIR, force: bool = False) -> Result:
    existing = [f for f in data_dir.glob("*abdc*") if f.suffix.lower() in
                (".xls", ".xlsx", ".csv")]
    if existing and not force and _fresh(existing[0], "abdc"):
        return Result("abdc", True, existing[0], f"Cached: {existing[0].name}.")

    link = _resolve_link(
        ABDC_PAGE, r"(jql|journal[-_ ]?quality[-_ ]?list).*\.(xlsx|xls|csv)"
    )
    if not link:
        if existing:
            return Result("abdc", True, existing[0], f"Using {existing[0].name}.")
        return Result(
            "abdc", False, None,
            "Could not find the JQL spreadsheet link on abdc.edu.au. Download "
            "it manually into data/ (any filename containing 'abdc').",
        )
    name = link.split("/")[-1].split("?")[0]
    if "abdc" not in name.lower():
        name = "abdc_" + name
    return _download(link, data_dir / name, "abdc")


FETCHERS = {
    "scimago": ("SCImago (SJR quartiles)", fetch_scimago),
    "scopus": ("Scopus source list", fetch_scopus),
    "pubmed": ("PubMed / MEDLINE serials", fetch_pubmed),
    "abdc": ("ABDC Journal Quality List", fetch_abdc),
}


def fetch_all(data_dir: Path = DATA_DIR, force: bool = False) -> list[Result]:
    return [fn(data_dir, force) for _, fn in FETCHERS.values()]
