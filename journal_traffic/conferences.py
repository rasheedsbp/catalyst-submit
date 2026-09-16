"""Conference shortlist for a paper title.

Four free sources, each answering a different question:

    OpenAlex   — which conference series actually publish this subject, and
                 how much, recently. The relevance signal. (free API)
    Scopus     — are its proceedings indexed? Read from the "All Conf.
                 Proceedings" and "Serial Conf. Proc." sheets of the
                 ext_list workbook already in data/. (offline)
    CORE       — the ICORE/CORE ranking (A*, A, B, C), the standard quality
                 signal for computing conferences. CSV export from
                 portal.core.edu.au, cached in data/. (free)
    WikiCFP    — the next edition: dates, location, submission deadline.
                 (free public pages)

Nothing is invented: a conference with no upcoming call found says so, and an
unranked conference is "unranked", not a guessed rank. Series run by known
predatory organisers are flagged, never silently kept.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from hashlib import sha1
from pathlib import Path

import requests

DATA_DIR = Path("data")
CACHE_DIR = Path("cache") / "conferences"
CACHE_TTL = 3 * 24 * 3600            # calls for papers change quickly

WIKICFP_SEARCH = "http://www.wikicfp.com/cfp/servlet/tool.search"
WIKICFP_BASE = "http://www.wikicfp.com"
CORE_EXPORT = ("https://portal.core.edu.au/conf-ranks/?search=&by=all&source=all"
               "&sort=arank&page=1&do=Export")
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# Organisers widely documented as running predatory "conferences". A match is a
# warning shown to the user, not a silent removal.
PREDATORY_ORGANISERS = [
    "waset", "world academy of science, engineering and technology",
    "iier", "institute for engineering research", "iraj", "iser",
    "international society for engineering research", "omics",
    "conference series llc", "scientific federation", "sciencekarma",
    "academics world", "the iier", "researchfora", "research fora",
    "theires", "iastem", "ierd",
]

MAJOR_PUBLISHERS = ("ieee", "acm", "springer", "elsevier", "iop", "aip",
                    "spie", "scitepress", "atlantis", "mdpi", "usenix", "aaai",
                    "iet", "asme", "curran")

ORDINAL = re.compile(r"\b\d{1,3}(?:st|nd|rd|th)\b", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
ROMAN_ED = re.compile(r"\b[IVXLC]{2,}\b")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def norm_name(s: str | None) -> str:
    """Series-level name: no year, edition number, acronym suffix or punctuation.

    "2025 IEEE 12th International Conference on Smart Grid (SmartGrid 2025)"
    and "IEEE International Conference on Smart Grid" normalise the same.
    """
    t = s if isinstance(s, str) else ""
    t = re.sub(r"\([^)]*\)", " ", t)
    # drop acronyms ("..., WCNC", "SmartGrid") but keep publisher names
    t = re.sub(r"\b(?!(?:IEEE|ACM|IET|SPIE|AIP|IFIP|ASME|IEICE|IAPR)\b)"
               r"[A-Z][A-Z0-9&\-]+\b", " ", t)
    t = t.lower()
    t = YEAR.sub(" ", t)
    t = ORDINAL.sub(" ", t)
    t = re.sub(r"\b(proceedings|proc\.?|of the|annual|conference record|"
               r"digest of technical papers)\b", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\b(the|a|an|and|of|on|in|for)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def acronym_of(s: str | None) -> str | None:
    """Acronym from "(ICASSP 2025)" or a leading "ICASSP 2025 - ..."."""
    if not s:
        return None
    m = re.search(r"\(([A-Z][A-Za-z0-9&\-]{1,15})(?:[\s'’]*\d{2,4})?\)", s)
    if m:
        return m.group(1).upper()
    m = re.match(r"^\s*([A-Z][A-Z0-9&\-]{1,15})\s*(?:['’]?\d{2,4})?\s*[:\-–]", s)
    return m.group(1).upper() if m else None


def _cache(key: str, fetch, ttl: int = CACHE_TTL):
    f = CACHE_DIR / (sha1(key.encode()).hexdigest()[:20] + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    value = fetch()
    if value is not None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(value), encoding="utf-8")
        except OSError:
            pass
    return value


@dataclass
class Conference:
    name: str
    acronym: str | None = None
    openalex_id: str | None = None
    publisher: str | None = None
    homepage: str | None = None
    papers_on_topic: int = 0
    works_recent: int = 0
    last_year: int | None = None
    h_index: int | None = None
    matched_terms: list[str] = field(default_factory=list)

    scopus_indexed: str = "Not found"        # "Yes" / "Discontinued" / "Not found"
    scopus_last_year: int | None = None
    core_rank: str | None = None
    core_source: str | None = None

    next_edition: str | None = None
    next_when: str | None = None
    next_where: str | None = None
    next_deadline: str | None = None
    days_to_deadline: int | None = None
    cfp_url: str | None = None
    website: str | None = None             # the conference's own page
    notification: str | None = None
    abstract_deadline: str | None = None
    status: str = "No call announced"      # Open / Closing soon / No call announced

    flags: list[str] = field(default_factory=list)
    score: float = 0.0

    def row(self) -> dict:
        d = asdict(self)
        d["matched_terms"] = "; ".join(self.matched_terms)
        d["flags"] = "; ".join(self.flags)
        return d


# --------------------------------------------------------------------------- #
# 1. OpenAlex: which conference series publish this subject
# --------------------------------------------------------------------------- #
def openalex_conferences(client, term: str, years: int = 5,
                         min_papers: int = 3) -> dict[str, int]:
    """source id -> papers on the term in the last `years` years."""
    y0 = date.today().year - years
    safe = re.sub(r"[,|:]", " ", term).strip()
    page = client._get(
        "/works",
        filter=(f"title_and_abstract.search:{safe},publication_year:>{y0},"
                "primary_location.source.type:conference"),
        group_by="primary_location.source.id",
        per_page=200,
    )
    out = {}
    for g in page.get("group_by", []) or []:
        key = g.get("key")
        if not key or key == "unknown":
            continue
        if g.get("count", 0) >= min_papers:
            out[key.rsplit("/", 1)[-1]] = int(g["count"])
    return out


def hydrate_sources(client, ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 50):
        batch = "|".join(ids[i:i + 50])
        page = client._get("/sources", filter=f"ids.openalex:{batch}", per_page=50)
        for s in page.get("results", []) or []:
            out[s["id"].rsplit("/", 1)[-1]] = s
    return out


def from_source(sid: str, src: dict, hits: int, term: str) -> Conference:
    counts = {c["year"]: c.get("works_count", 0) for c in src.get("counts_by_year", []) or []}
    recent = sum(v for y, v in counts.items() if y >= date.today().year - 3)
    last = max((y for y, v in counts.items() if v), default=None)
    name = src.get("display_name") or ""
    return Conference(
        name=name,
        acronym=acronym_of(name) or (src.get("abbreviated_title") or None),
        openalex_id=sid,
        publisher=src.get("host_organization_name"),
        homepage=src.get("homepage_url"),
        papers_on_topic=hits,
        works_recent=recent,
        last_year=last,
        h_index=(src.get("summary_stats") or {}).get("h_index"),
        matched_terms=[term],
    )


# --------------------------------------------------------------------------- #
# 2. Scopus proceedings (offline, from the ext_list workbook)
# --------------------------------------------------------------------------- #
class ScopusProceedings:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.by_name: dict[str, int] = {}          # normalised series -> last year
        self.serial_issn: dict[str, str] = {}      # issn -> title
        self.discontinued: set[str] = set()
        self.source_file: Path | None = None
        self._load(data_dir)

    def _load(self, data_dir: Path) -> None:
        import pickle
        files = sorted(data_dir.glob("ext_list_*.xls*"),
                       key=lambda f: f.stat().st_mtime, reverse=True) \
            if data_dir.exists() else []
        if not files:
            return
        src = files[0]
        self.source_file = src
        cache = src.parent / f".confproc2_{src.stem}.pkl"
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            try:
                self.by_name, self.serial_issn, self.discontinued = \
                    pickle.loads(cache.read_bytes())
                return
            except Exception:  # noqa: BLE001
                pass
        import pandas as pd
        try:
            xl = pd.ExcelFile(src)
        except Exception:  # noqa: BLE001
            return
        for sheet in xl.sheet_names:
            low = sheet.lower()
            if "conf" not in low:
                continue
            try:
                df = pd.read_excel(xl, sheet_name=sheet, dtype=str)
            except Exception:  # noqa: BLE001
                continue
            title_col = next((c for c in df.columns if str(c).strip().lower()
                              == "source title"), None)
            if title_col is None:
                continue
            year_col = next((c for c in df.columns if str(c).strip().lower() == "year"), None)
            disc_col = next((c for c in df.columns if "discontinued" in str(c).lower()), None)
            df = df.where(df.notna(), None)
            for r in df.to_dict("records"):          # iterrows is ~10x slower
                key = norm_name(r.get(title_col))
                if not key:
                    continue
                yr = None
                if year_col is not None:
                    try:
                        yr = int(float(r.get(year_col)))
                    except (TypeError, ValueError):
                        yr = None
                else:
                    cov = str(r.get("Coverage") or "")
                    ys = [int(y) for y in re.findall(r"(?:19|20)\d{2}", cov)]
                    yr = max(ys) if ys else None
                self.by_name[key] = max(self.by_name.get(key) or 0, yr or 0) or None
                if disc_col is not None and str(r.get(disc_col) or "").strip() not in ("", "nan"):
                    self.discontinued.add(key)
                for col in ("ISSN", "EISSN"):
                    v = re.sub(r"[^0-9Xx]", "", str(r.get(col) or ""))
                    if v and v.lower() != "nan":
                        v = v.split(".")[0].zfill(8)[:8]
                        self.serial_issn[v] = key
        try:
            cache.write_bytes(pickle.dumps((self.by_name, self.serial_issn,
                                            self.discontinued)))
        except OSError:
            pass

    @property
    def available(self) -> bool:
        return bool(self.by_name)

    def lookup(self, name: str, issns: list[str] | None = None) -> tuple[str, int | None]:
        keys = []
        for i in issns or []:
            k = self.serial_issn.get(re.sub(r"[^0-9Xx]", "", i).zfill(8)[:8])
            if k:
                keys.append(k)
        n = norm_name(name)
        if n:
            keys.append(n)
        for k in keys:
            if k in self.by_name:
                status = "Discontinued" if k in self.discontinued else "Yes"
                return status, self.by_name[k]
        # strict containment fallback: the series name inside a longer
        # proceedings title, requiring a long enough name to be specific
        if n and len(n.split()) >= 4:
            for k, yr in self.by_name.items():
                if n in k or (k in n and len(k.split()) >= 4):
                    return ("Discontinued" if k in self.discontinued else "Yes"), yr
        return "Not found", None

    def summary(self) -> str:
        if not self.available:
            return "Scopus conference proceedings: no ext_list workbook in data/."
        return (f"Scopus conference proceedings: {len(self.by_name):,} series "
                f"from {self.source_file.name}")


# --------------------------------------------------------------------------- #
# 3. CORE ranking
# --------------------------------------------------------------------------- #
class CoreRanking:
    """ICORE/CORE conference ranks from the portal's CSV export."""

    FILE = DATA_DIR / "core_conference_ranks.csv"

    def __init__(self, data_dir: Path = DATA_DIR, fetch: bool = True):
        self.by_acronym: dict[str, tuple[str, str]] = {}
        self.by_name: dict[str, tuple[str, str]] = {}
        path = data_dir / self.FILE.name
        if not path.exists() and fetch:
            self.download(path)
        if path.exists():
            self._parse(path.read_text(encoding="utf-8", errors="ignore"))

    @staticmethod
    def download(path: Path) -> bool:
        try:
            r = requests.get(CORE_EXPORT, headers=UA, timeout=30)
            if r.ok and r.text.count("\n") > 100:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(r.text, encoding="utf-8")
                return True
        except requests.RequestException:
            pass
        return False

    def _parse(self, text: str) -> None:
        # export rows: id, title, acronym, source (e.g. ICORE2026), rank, ...
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 5:
                continue
            title, acr, source, rank = row[1], row[2], row[3], row[4]
            rank = rank.strip()
            if rank not in ("A*", "A", "B", "C"):
                continue                       # skip "Unranked", "National", etc.
            val = (rank, source.strip())
            if acr.strip():
                self.by_acronym.setdefault(acr.strip().upper(), val)
            if title.strip():
                self.by_name.setdefault(norm_name(title), val)

    @property
    def available(self) -> bool:
        return bool(self.by_name)

    def lookup(self, name: str, acronym: str | None) -> tuple[str | None, str | None]:
        n = norm_name(name)
        if n in self.by_name:
            return self.by_name[n]
        # acronyms collide across fields, so only trust one when the name agrees
        if acronym and acronym.upper() in self.by_acronym:
            rank, src = self.by_acronym[acronym.upper()]
            for k, v in self.by_name.items():
                if v == (rank, src) and (k in n or n in k):
                    return rank, src
        return None, None

    def summary(self) -> str:
        return (f"CORE conference ranks: {len(self.by_name):,} ranked conferences"
                if self.available else
                "CORE conference ranks: not available (download failed or file "
                "missing — save the CORE portal CSV export as "
                "data/core_conference_ranks.csv)")


# --------------------------------------------------------------------------- #
# 4. WikiCFP: upcoming editions
# --------------------------------------------------------------------------- #
@dataclass
class CallForPapers:
    acronym: str
    name: str
    when: str
    where: str
    deadline: str
    url: str
    deadline_date: str | None = None
    # filled from the event page
    website: str | None = None
    notification: str | None = None
    camera_ready: str | None = None
    abstract_deadline: str | None = None


def _parse_date(s: str) -> date | None:
    s = re.sub(r"\(.*?\)", "", s or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_wikicfp(html: str) -> list[CallForPapers]:
    """Rows from a WikiCFP search results page.

    Each event is two table rows: the first holds the linked acronym and the
    full name, the second holds When / Where / Deadline.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    out: list[CallForPapers] = []
    cells = lambda r: [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                       for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S | re.I)]
    i = 0
    while i < len(rows) - 1:
        link = re.search(r'href="(/cfp/servlet/event\.showcfp\?[^"]+)"', rows[i])
        first = cells(rows[i])
        if link and len(first) >= 2:
            second = cells(rows[i + 1])
            if len(second) >= 3:
                acr, name = first[0], first[1]
                dl = second[2]
                d = _parse_date(dl)
                out.append(CallForPapers(
                    acronym=acr, name=name, when=second[0], where=second[1],
                    deadline=dl, url=WIKICFP_BASE + link.group(1).replace("&amp;", "&"),
                    deadline_date=d.isoformat() if d else None))
                i += 2
                continue
        i += 1
    return out


def parse_wikicfp_event(html: str) -> dict:
    """Official website and key dates from a WikiCFP event page."""
    out: dict = {}
    m = re.search(r"Link:\s*<a[^>]+href=\"(https?://[^\"]+)\"", html, re.I)
    if m:
        out["website"] = m.group(1).strip()
    text_rows = re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", html, re.S | re.I)
    for k, v in text_rows:
        key = re.sub(r"<[^>]+>|\s+", " ", k).strip().lower()
        val = re.sub(r"<[^>]+>|\s+", " ", v).strip()
        if not val:
            continue
        if key.startswith("abstract registration"):
            out["abstract_deadline"] = val
        elif key.startswith("submission deadline"):
            out["deadline"] = val
        elif key.startswith("notification"):
            out["notification"] = val
        elif key.startswith("final version"):
            out["camera_ready"] = val
    return out


def wikicfp_event(url: str) -> dict:
    def fetch():
        try:
            r = requests.get(url, headers=UA, timeout=15)
        except requests.RequestException:
            return None
        return parse_wikicfp_event(r.text) if r.ok else None
    return _cache(f"wikicfp-event:{url}", fetch, ttl=7 * 24 * 3600) or {}


def enrich_calls(calls: list[CallForPapers], workers: int = 8) -> None:
    """Add website, notification date and the authoritative deadline in place."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        details = list(pool.map(lambda c: wikicfp_event(c.url), calls))
    for c, d in zip(calls, details):
        c.website = d.get("website") or c.website
        c.notification = d.get("notification") or c.notification
        c.camera_ready = d.get("camera_ready") or c.camera_ready
        c.abstract_deadline = d.get("abstract_deadline") or c.abstract_deadline
        if d.get("deadline"):
            dd = _parse_date(d["deadline"])
            if dd:                       # event page may show an extended deadline
                c.deadline, c.deadline_date = d["deadline"], dd.isoformat()


def is_open(c: CallForPapers, today: date | None = None) -> bool:
    today = today or date.today()
    return bool(c.deadline_date) and date.fromisoformat(c.deadline_date) >= today


def wikicfp_search(term: str) -> list[CallForPapers]:
    def fetch():
        try:
            r = requests.get(WIKICFP_SEARCH, params={"q": term, "year": "f"},
                             headers=UA, timeout=15)
        except requests.RequestException:
            return None
        if not r.ok:
            return None
        return [asdict(c) for c in parse_wikicfp(r.text)]
    data = _cache(f"wikicfp:{term.lower()}", fetch) or []
    return [CallForPapers(**d) for d in data]


def _cfp_matches(conf: Conference, cfp: CallForPapers) -> bool:
    acr = re.sub(r"\s*['’]?\d{2,4}\s*$", "", cfp.acronym).strip().upper()
    if conf.acronym and acr == conf.acronym.upper():
        return True
    a, b = norm_name(conf.name), norm_name(cfp.name)
    if not a or not b:
        return False
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / max(len(wa | wb), 1) >= 0.7


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #
def _flags(c: Conference) -> list[str]:
    f = []
    blob = f"{c.name} {c.publisher or ''} {c.homepage or ''}".lower()
    if any(p in blob for p in PREDATORY_ORGANISERS):
        f.append("organiser on predatory-conference watch lists")
    if c.last_year and c.last_year < date.today().year - 2:
        f.append(f"no proceedings since {c.last_year}")
    if c.scopus_indexed == "Discontinued":
        f.append("Scopus coverage discontinued")
    if c.days_to_deadline is not None and 0 <= c.days_to_deadline < 14:
        f.append(f"deadline in {c.days_to_deadline} days")
    return f


def _score(c: Conference, max_hits: int) -> float:
    rel = c.papers_on_topic / max(max_hits, 1)                          # 0..1
    idx = {"Yes": 1.0, "Discontinued": 0.0}.get(c.scopus_indexed, 0.3)
    rank = {"A*": 1.0, "A": 0.85, "B": 0.6, "C": 0.4}.get(c.core_rank or "", 0.3)
    pub = 1.0 if any(p in (c.publisher or "").lower() for p in MAJOR_PUBLISHERS) else 0.4
    open_call = 1.0 if (c.days_to_deadline is not None and c.days_to_deadline >= 0) else 0.3
    s = 40 * rel + 20 * idx + 15 * rank + 10 * pub + 15 * open_call
    if any("predatory" in x for x in c.flags):
        s *= 0.2
    return round(s, 1)


def find_conferences(title: str, terms: list[str] | None = None, client=None,
                     limit: int = 25, require_scopus: bool = False,
                     upcoming_only: bool = False, min_rank: str | None = None,
                     progress=print) -> list[Conference]:
    from .sources import OpenAlexClient
    client = client or OpenAlexClient()
    if not terms:
        from .terms import terms_from_title
        terms = [t.text for t in terms_from_title(client, title)][:4]
    progress(f"Search terms: {', '.join(terms)}")

    hits: dict[str, int] = {}
    term_of: dict[str, list[str]] = {}
    for t in terms:
        try:
            got = openalex_conferences(client, t)
        except Exception as exc:  # noqa: BLE001
            progress(f"  OpenAlex search failed for '{t}': {type(exc).__name__}")
            continue
        progress(f"  '{t}': {len(got)} conference series")
        for sid, n in got.items():
            hits[sid] = hits.get(sid, 0) + n
            term_of.setdefault(sid, []).append(t)

    ids = sorted(hits, key=hits.get, reverse=True)[: max(limit * 3, 60)]
    sources = hydrate_sources(client, ids) if ids else {}
    confs = []
    for sid in ids:
        if sid not in sources:
            continue
        c = from_source(sid, sources[sid], hits[sid], term_of[sid][0])
        c.matched_terms = term_of[sid]
        confs.append(c)

    scopus = ScopusProceedings()
    core = CoreRanking()
    progress("  " + scopus.summary())
    progress("  " + core.summary())
    for c in confs:
        issns = (sources.get(c.openalex_id) or {}).get("issn") or []
        c.scopus_indexed, c.scopus_last_year = scopus.lookup(c.name, issns) \
            if scopus.available else ("Unknown (no Scopus list)", None)
        c.core_rank, c.core_source = core.lookup(c.name, c.acronym)

    progress("Looking for upcoming calls for papers (WikiCFP)")
    today = date.today()
    cfps: list[CallForPapers] = []
    for t in terms:
        cfps.extend(wikicfp_search(t))
    matched: dict[int, list[CallForPapers]] = {}
    for i, c in enumerate(confs):
        cands = [x for x in cfps if _cfp_matches(c, x)]
        if cands:
            matched[i] = cands
            if not c.acronym:
                c.acronym = re.sub(r"\s*\d{2,4}$", "", cands[0].acronym)
    # event pages carry the official website and any extended deadline
    to_enrich = {id(x): x for v in matched.values() for x in v if is_open(x, today)}
    if to_enrich:
        progress(f"  Reading {len(to_enrich)} call-for-papers page(s) for links and dates")
        enrich_calls(list(to_enrich.values()))
    passed = 0
    for i, c in enumerate(confs):
        cands = matched.get(i, [])
        future = [x for x in cands if is_open(x, today)]
        if cands and not future:
            c.status = "Deadline passed"
            passed += 1
            continue
        if not future:
            continue
        pick = min(future, key=lambda x: x.deadline_date)
        c.next_edition, c.next_when, c.next_where = pick.acronym, pick.when, pick.where
        c.next_deadline, c.cfp_url = pick.deadline, pick.url
        c.website = pick.website or c.homepage
        c.notification, c.abstract_deadline = pick.notification, pick.abstract_deadline
        c.days_to_deadline = (date.fromisoformat(pick.deadline_date) - today).days
        c.status = "Closing soon" if c.days_to_deadline <= 14 else "Open"
    if passed:
        progress(f"  {passed} conference(s) omitted: this year's deadline has passed")
    # the passed ones never reach the shortlist
    confs = [c for c in confs if c.status != "Deadline passed"]
    for c in confs:
        c.website = c.website or c.homepage

    order = ["A*", "A", "B", "C"]
    if require_scopus:
        confs = [c for c in confs if c.scopus_indexed == "Yes"]
    if min_rank in order:
        allowed = set(order[: order.index(min_rank) + 1])
        confs = [c for c in confs if c.core_rank in allowed]
    if upcoming_only:
        confs = [c for c in confs if c.status in ("Open", "Closing soon")]

    max_hits = max((c.papers_on_topic for c in confs), default=1)
    for c in confs:
        c.flags = _flags(c)
        c.score = _score(c, max_hits)
    confs.sort(key=lambda c: c.score, reverse=True)
    progress(f"{len(confs)} conference(s) after filters")
    return confs[:limit]


def upcoming_calls(terms: list[str]) -> list[CallForPapers]:
    """Open calls for the terms, even for conferences OpenAlex has not indexed
    yet (new series have no proceedings to count)."""
    today = date.today()
    seen, out = set(), []
    for t in terms:
        for c in wikicfp_search(t):
            if c.url in seen:
                continue
            seen.add(c.url)
            if is_open(c, today):
                out.append(c)
    enrich_calls(out)
    out = [c for c in out if is_open(c, today)]      # recheck after enrichment
    return sorted(out, key=lambda c: c.deadline_date or "9999")
