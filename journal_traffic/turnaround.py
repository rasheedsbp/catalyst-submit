"""Measured turnaround time, from Crossref date assertions.

Why not the declared figure
---------------------------
Journals rarely publish "time to first decision", and when they do it is a
marketing number. Across 132 real journals in a recent run, zero had one.

Crossref carries something better. Most major publishers deposit the article's
received / revised / accepted dates alongside the DOI, as `assertion` entries
or in the `journal-issue` metadata. Those are per-article facts, so sampling
50 recent articles from a journal gives a measured distribution rather than a
claim.

What this yields per journal:

    submission_to_acceptance   days from received to accepted  (peer review)
    acceptance_to_publication  days from accepted to online    (production)
    total_turnaround           received to online

The median is reported, not the mean: turnaround distributions have long right
tails (one paper stuck in revision for two years) that drag a mean into
uselessness. The interquartile range is reported too, because a journal with a
tight spread is more predictable than one with the same median and a wide one —
and predictability is what a client on a deadline actually needs.

Coverage is partial. Roughly half of publishers deposit these dates. Journals
without them get None, and the recommender treats that as unknown rather than
assuming a value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import requests

CROSSREF = "https://api.crossref.org"
DEFAULT_CONTACT = "rasheedphd123@gmail.com"

# Assertion labels publishers use for the same three events. Case-insensitive.
RECEIVED_KEYS = {"received", "date-received", "submitted", "submission"}
ACCEPTED_KEYS = {"accepted", "date-accepted", "acceptance"}
REVISED_KEYS = {"revised", "date-revised", "revision-received"}


@dataclass
class Turnaround:
    """Measured turnaround for one journal, in days."""

    issn: str
    n_articles: int = 0          # articles sampled
    n_with_dates: int = 0        # of those, how many carried usable dates

    review_median: float | None = None      # received -> accepted
    review_p25: float | None = None
    review_p75: float | None = None

    production_median: float | None = None  # accepted -> published online
    total_median: float | None = None       # received -> published online

    @property
    def coverage(self) -> float:
        return self.n_with_dates / self.n_articles if self.n_articles else 0.0

    @property
    def is_reliable(self) -> bool:
        """Enough articles carried dates to trust the median."""
        return self.n_with_dates >= 8

    @property
    def spread(self) -> float | None:
        """Interquartile range — how predictable the journal is."""
        if self.review_p25 is None or self.review_p75 is None:
            return None
        return self.review_p75 - self.review_p25

    def describe(self) -> str:
        if not self.is_reliable:
            return f"turnaround unknown ({self.n_with_dates} dated articles)"
        parts = [f"review ~{self.review_median:.0f}d"]
        if self.spread is not None:
            parts.append(f"(IQR {self.review_p25:.0f}-{self.review_p75:.0f})")
        if self.production_median is not None:
            parts.append(f"production ~{self.production_median:.0f}d")
        return " ".join(parts)


def _parse_parts(parts) -> datetime | None:
    """Crossref date-parts: [[2024, 3, 15]] or [[2024, 3]] or [[2024]]."""
    if not parts:
        return None
    if isinstance(parts, dict):
        parts = parts.get("date-parts")
    if not parts or not isinstance(parts, list):
        return None
    first = parts[0] if isinstance(parts[0], list) else parts
    nums = [int(x) for x in first if isinstance(x, (int, float))]
    if not nums:
        return None
    y = nums[0]
    m = nums[1] if len(nums) > 1 else 1
    d = nums[2] if len(nums) > 2 else 1
    try:
        return datetime(y, min(max(m, 1), 12), min(max(d, 1), 28))
    except ValueError:
        return None


def _dates_from_work(work: dict) -> dict[str, datetime]:
    """Pull received / accepted / published out of one Crossref record."""
    out: dict[str, datetime] = {}

    # Publishers deposit these as free-form assertions.
    for a in work.get("assertion", []) or []:
        label = str(a.get("name", "")).lower().strip()
        raw = a.get("value")
        parsed = None
        if raw:
            for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%Y/%m/%d"):
                try:
                    parsed = datetime.strptime(str(raw).strip(), fmt)
                    break
                except ValueError:
                    continue
        if not parsed:
            continue
        if label in RECEIVED_KEYS:
            out["received"] = parsed
        elif label in ACCEPTED_KEYS:
            out["accepted"] = parsed
        elif label in REVISED_KEYS:
            out["revised"] = parsed

    # Some publishers use structured fields instead.
    for key, field in (("received", "received"), ("accepted", "accepted")):
        if key not in out and field in work:
            parsed = _parse_parts(work.get(field))
            if parsed:
                out[key] = parsed

    published = (
        _parse_parts(work.get("published-online"))
        or _parse_parts(work.get("published"))
        or _parse_parts(work.get("published-print"))
    )
    if published:
        out["published"] = published

    return out


class TurnaroundTracker:
    def __init__(self, mailto: str | None = None, pause: float = 0.12):
        self.session = requests.Session()
        self.mailto = mailto or DEFAULT_CONTACT
        self.pause = pause

    def measure(self, issn: str, sample: int = 60) -> Turnaround:
        """Sample recent articles and compute turnaround percentiles."""
        issn = str(issn).strip()
        result = Turnaround(issn=issn)
        if not issn:
            return result

        y_from = date.today().year - 3
        params = {
            "filter": f"type:journal-article,from-pub-date:{y_from}-01-01",
            "rows": sample,
            "sort": "published",
            "order": "desc",
            "select": "DOI,assertion,published-online,published-print,published",
            "mailto": self.mailto,
        }

        try:
            r = self.session.get(
                f"{CROSSREF}/journals/{issn}/works", params=params, timeout=25
            )
            if r.status_code != 200:
                return result
            items = r.json().get("message", {}).get("items", [])
        except (requests.RequestException, ValueError):
            return result
        finally:
            time.sleep(self.pause)

        result.n_articles = len(items)

        review, production, total = [], [], []
        for w in items:
            d = _dates_from_work(w)
            rec, acc, pub = d.get("received"), d.get("accepted"), d.get("published")

            if rec and acc:
                days = (acc - rec).days
                if 0 < days < 1500:          # guard against typos in deposits
                    review.append(days)
            if acc and pub:
                days = (pub - acc).days
                if 0 <= days < 1000:
                    production.append(days)
            if rec and pub:
                days = (pub - rec).days
                if 0 < days < 2000:
                    total.append(days)

        result.n_with_dates = len(review)

        if review:
            result.review_median = float(np.median(review))
            result.review_p25 = float(np.percentile(review, 25))
            result.review_p75 = float(np.percentile(review, 75))
        if production:
            result.production_median = float(np.median(production))
        if total:
            result.total_median = float(np.median(total))

        return result


def measure_many(
    issns: list[tuple[str, str]],
    workers: int = 10,
    progress: bool = True,
) -> dict[str, Turnaround]:
    """Measure many journals concurrently. issns is [(key, issn), ...]."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tracker = TurnaroundTracker()
    results: dict[str, Turnaround] = {}
    if not issns:
        return results

    total = len(issns)
    done = measured = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(tracker.measure, issn): key for key, issn in issns if issn
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                t = fut.result()
            except Exception:  # noqa: BLE001
                t = Turnaround(issn="")
            results[key] = t
            done += 1
            if t.is_reliable:
                measured += 1
            if progress and (done % 15 == 0 or done == total):
                print(
                    f"    turnaround {done}/{total}, "
                    f"{measured} with usable dates",
                    flush=True,
                )

    return results
