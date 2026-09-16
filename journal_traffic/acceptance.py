"""Harvest declared acceptance rates — these are the training labels.

The model is only as good as this file's yield. Elsevier and Springer publish
acceptance rate and time-to-first-decision on many journal homepages; some
journals also state submissions received outright in an annual editorial,
which lets us skip estimation entirely for those titles.

Anything found here is tier A or B evidence. Journals with nothing found get
a model prediction (tier C) or a quartile prior (tier D).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

# Publisher CDNs block obvious bot user-agents at the edge. These headers are
# what a normal browser sends; without them most requests never reach HTML.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Patterns are deliberately conservative — a false label poisons training.
RATE_PATTERNS = [
    re.compile(r"acceptance\s+rate[^0-9%]{0,40}?(\d{1,3}(?:\.\d)?)\s*%", re.I),
    re.compile(r"(\d{1,3}(?:\.\d)?)\s*%[^.]{0,30}?acceptance\s+rate", re.I),
    re.compile(r"accepts?\s+(?:approximately\s+)?(\d{1,3})\s*%\s+of\s+submissions", re.I),
    re.compile(r"acceptance\s+ratio[^0-9%]{0,40}?(\d{1,3}(?:\.\d)?)\s*%", re.I),
    re.compile(r"(\d{1,3})\s*%\s+(?:of\s+)?(?:submitted\s+)?"
               r"(?:manuscript|paper|article)s?\s+(?:are\s+)?accepted", re.I),
    re.compile(r"rejection\s+rate[^0-9%]{0,40}?(\d{1,3}(?:\.\d)?)\s*%", re.I),
]

# Index of the pattern above that reports REJECTION, needing inversion.
REJECTION_PATTERN_INDEX = 5

SUBMISSION_PATTERNS = [
    re.compile(
        r"(?:received|receives)\s+(?:approximately\s+|about\s+|over\s+)?"
        r"([\d,]{3,7})\s+(?:new\s+)?(?:manuscript|submission|paper)s",
        re.I,
    ),
    re.compile(r"([\d,]{3,7})\s+(?:manuscript|submission)s\s+(?:were\s+)?received", re.I),
]

DECISION_PATTERN = re.compile(
    r"(?:time to first decision|first decision)[^0-9]{0,40}?(\d{1,3})\s*(?:days|weeks)", re.I
)


@dataclass
class AcceptanceEvidence:
    acceptance_rate: float | None = None
    submissions: int | None = None
    days_to_first_decision: int | None = None
    stated_speed: dict | None = None      # see speed.parse_stated_speed
    source_url: str | None = None
    tier: str | None = None  # "A" declared submissions, "B" declared rate


# Populated as a run proceeds so the CLI can report WHY nothing was found.
FETCH_STATS: dict[str, int] = {}


def _record(outcome: str) -> None:
    FETCH_STATS[outcome] = FETCH_STATS.get(outcome, 0) + 1


def _fetch(url: str, timeout: int = 8) -> str | None:
    """Fetch a page, recording the outcome.

    The previous version returned None identically for a 403, a timeout and a
    perfectly good page with no rate on it — which made a zero-yield run
    impossible to diagnose.
    """
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    except requests.Timeout:
        _record("timeout")
        return None
    except requests.RequestException:
        _record("connection_error")
        return None

    if r.status_code == 403:
        _record("blocked_403")
        return None
    if r.status_code == 404:
        _record("not_found_404")
        return None
    if r.status_code != 200:
        _record(f"http_{r.status_code}")
        return None
    if "text/html" not in r.headers.get("content-type", ""):
        _record("not_html")
        return None

    # A CAPTCHA or bot-check page returns 200 with HTML, so status alone is
    # not enough to call this a success.
    low = r.text[:4000].lower()
    if any(t in low for t in ("captcha", "are you a robot",
                              "enable javascript and cookies",
                              "access denied", "cloudflare")):
        _record("bot_challenge")
        return None

    _record("ok")
    return r.text


def fetch_report() -> str:
    """Human-readable summary of why the scrape did or didn't work."""
    if not FETCH_STATS:
        return "No pages were fetched."
    total = sum(FETCH_STATS.values())
    parts = ", ".join(
        f"{k}={v}" for k, v in sorted(FETCH_STATS.items(), key=lambda kv: -kv[1])
    )
    lines = [f"Page fetches: {total} ({parts})"]

    ok = FETCH_STATS.get("ok", 0)
    blocked = (FETCH_STATS.get("blocked_403", 0)
               + FETCH_STATS.get("bot_challenge", 0))
    if blocked > total * 0.3:
        lines.append(
            "  Most requests were blocked by publisher bot protection. This is "
            "expected on Elsevier and Wiley; it is not a bug you can fix from "
            "here. Use --labels with rates you know instead."
        )
    elif ok > total * 0.5 and ok:
        lines.append(
            "  Pages loaded fine but stated no acceptance rate — most journals "
            "simply do not publish one. Springer and MDPI titles are the most "
            "likely to."
        )
    return "\n".join(lines)


def _clean_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def scrape_journal_page(url: str) -> AcceptanceEvidence:
    ev = AcceptanceEvidence(source_url=url)
    html = _fetch(url)
    if not html:
        return ev

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    for pat in SUBMISSION_PATTERNS:
        m = pat.search(text)
        if m:
            n = _clean_int(m.group(1))
            if 50 <= n <= 100_000:  # sanity band
                ev.submissions = n
                ev.tier = "A"
            break

    for idx, pat in enumerate(RATE_PATTERNS):
        m = pat.search(text)
        if m:
            rate = float(m.group(1)) / 100
            if idx == REJECTION_PATTERN_INDEX:
                rate = 1 - rate  # page quoted a rejection rate
            if 0.01 <= rate <= 0.95:
                ev.acceptance_rate = rate
                ev.tier = ev.tier or "B"
            break

    m = DECISION_PATTERN.search(text)
    if m:
        ev.days_to_first_decision = int(m.group(1))

    from .speed import parse_stated_speed
    ev.stated_speed = parse_stated_speed(text) or None

    return ev


# Publisher-specific subpages where rates live more often than on the homepage.
SUBPAGE_HINTS = {
    "sciencedirect": ["/about/insights", "/publish/guide-for-authors"],
    "elsevier": ["/about/insights"],
    "springer": ["/journal-metrics", "/submission-guidelines"],
    "link.springer": ["/journal-metrics"],
    "tandfonline": ["/journal-information", "/aims-scope"],
    "wiley": ["/journal-metrics", "/about"],
    "mdpi": ["/stats", "/about"],
    "sagepub": ["/journal-metrics"],
    "emerald": ["/author-guidelines"],
    "hindawi": ["/about"],
    "frontiersin": ["/about"],
}


def candidate_urls(homepage: str | None) -> list[str]:
    """Homepage plus the publisher's usual metrics subpages."""
    if not homepage:
        return []
    base = homepage.rstrip("/")
    urls = [base]
    low = base.lower()
    for key, paths in SUBPAGE_HINTS.items():
        if key in low:
            urls.extend(base + pth for pth in paths)
            break
    return urls


def harvest(homepage: str | None, extra_urls: list[str] | None = None) -> AcceptanceEvidence:
    """Try the homepage, then publisher-specific metrics pages."""
    candidates = candidate_urls(homepage) + [u for u in (extra_urls or []) if u]
    best = AcceptanceEvidence()
    speed: dict = {}
    for url in candidates:
        ev = scrape_journal_page(url)
        for k, v in (ev.stated_speed or {}).items():
            speed.setdefault(k, v)            # metrics subpages add to the homepage
        # Tier A beats tier B beats nothing.
        if ev.tier == "A":
            ev.stated_speed = speed or None
            return ev
        if ev.tier == "B" and best.tier is None:
            best = ev
        elif ev.days_to_first_decision and best.days_to_first_decision is None:
            best.days_to_first_decision = ev.days_to_first_decision
    best.stated_speed = speed or None
    return best


def harvest_many(
    homepages: list[tuple[str, str | None]],
    workers: int = 12,
    progress: bool = True,
) -> dict[str, AcceptanceEvidence]:
    """Harvest many journals concurrently.

    Scraping is almost entirely waiting on the network, so it parallelises
    cleanly. Sequentially this was the dominant cost of a run — 120 journals
    x 3 URLs x a 6s timeout is 36 minutes of mostly idle waiting; at 12
    workers it is about 3.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, AcceptanceEvidence] = {}
    if not homepages:
        return results

    total = len(homepages)
    done = 0
    found = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(harvest, home): key for key, home in homepages
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                ev = fut.result()
            except Exception:  # noqa: BLE001
                ev = AcceptanceEvidence()
            results[key] = ev
            done += 1
            if ev.tier:
                found += 1
            if progress and (done % 10 == 0 or done == total):
                print(
                    f"    scraped {done}/{total} homepages, "
                    f"{found} acceptance rate(s) found",
                    flush=True,
                )

    return results
