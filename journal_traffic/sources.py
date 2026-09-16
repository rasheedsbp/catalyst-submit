"""Data sources: OpenAlex for article counts and journal metadata.

OpenAlex is free and needs no API key. Supplying a contact email in the
`mailto` parameter puts you in the polite pool (faster, more reliable).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Iterator

import requests

OPENALEX = "https://api.openalex.org"

# Sent as the `mailto` parameter on every request. This does NOT email you —
# it is a courtesy convention that puts you in OpenAlex's faster, higher-limit
# pool. Edit it if you want, or blank it out to use the shared pool.
DEFAULT_CONTACT = "rasheedphd123@gmail.com"

# Responses are cached on disk for a week. A 60-journal run makes a few hundred
# requests; without caching, every rerun (including after a crash) repeats all
# of them. Delete ./cache/ to force a refresh.
CACHE_DIR = Path("cache")
CACHE_TTL_SECONDS = 7 * 24 * 3600

# Article types that represent peer-reviewed research going through the
# regular submission track. Editorials, errata and letters are excluded
# because they inflate the numerator without corresponding submissions.
RESEARCH_TYPES = {"article", "review"}


@dataclass
class JournalRecord:
    """Everything we know about one journal before prediction."""

    openalex_id: str
    display_name: str
    on_topic_hits: int = 0
    subject_share: float | None = None
    issn_l: str | None = None
    issns: list[str] = field(default_factory=list)
    publisher: str | None = None
    is_oa: bool = False
    apc_usd: int | None = None
    apc_prices: list[dict] = field(default_factory=list)
    is_in_doaj: bool = False
    homepage: str | None = None

    # Volume signals
    works_by_year: dict[int, int] = field(default_factory=dict)
    research_works_by_year: dict[int, int] = field(default_factory=dict)

    # Impact / selectivity signals
    cited_by_count: int = 0
    h_index: int | None = None
    two_yr_mean_citedness: float | None = None

    # Concentration signals
    special_issue_share: float | None = None
    intl_author_share: float | None = None
    top_country_share: float | None = None

    # Ground truth, when we can find it
    declared_acceptance_rate: float | None = None
    declared_submissions: int | None = None
    acceptance_source: str | None = None


class OpenAlexClient:
    def __init__(
        self,
        mailto: str | None = None,
        pause: float = 0.12,
        use_cache: bool = True,
    ):
        self.session = requests.Session()
        self.mailto = mailto or DEFAULT_CONTACT
        self.pause = pause
        self.use_cache = use_cache
        self.last_relevance: dict[str, dict] = {}

    def _cache_path(self, path: str, params: dict) -> Path:
        key = sha1(
            (path + json.dumps(params, sort_keys=True)).encode()
        ).hexdigest()[:20]
        return CACHE_DIR / f"{key}.json"

    def _get(self, path: str, **params) -> dict:
        if self.mailto:
            params["mailto"] = self.mailto

        cache_file = self._cache_path(path, params) if self.use_cache else None
        if cache_file and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass  # corrupt entry — fall through and refetch

        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.session.get(
                    f"{OPENALEX}{path}", params=params, timeout=30
                )
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            data = resp.json()

            if cache_file:
                try:
                    CACHE_DIR.mkdir(exist_ok=True)
                    cache_file.write_text(
                        json.dumps(data), encoding="utf-8"
                    )
                except OSError:
                    pass  # cache is an optimisation, never load-bearing

            time.sleep(self.pause)
            return data

        if last_exc:
            raise last_exc
        raise requests.HTTPError(
            f"OpenAlex did not respond successfully after 4 attempts: {path}"
        )

    # ---------------- discovery ----------------

    def journals_by_subject(
        self,
        subject: str,
        limit: int = 100,
        min_works_per_year: int = 20,
        country: str | None = None,
        debug: bool = False,
    ) -> list[dict]:
        """Find journals in a subject area.

        OpenAlex has been migrating from `concepts` to `topics`, and
        source-level subject tagging is inconsistent. So we try four
        strategies in order of quality and take the first that returns
        anything. Strategy 3 is the workhorse: it asks which journals
        actually publish papers on the subject, which never depends on a
        journal being tagged correctly.
        """
        strategies = [
            ("works group_by (most reliable)", self._by_works_groupby),
            ("topic filter", self._by_topic),
            ("concept filter", self._by_concept),
            ("source name search", self._by_name_search),
        ]

        for label, fn in strategies:
            try:
                ids_or_sources = fn(subject, limit, min_works_per_year)
            except Exception as exc:  # noqa: BLE001
                if debug:
                    print(f"  [discovery] {label}: error {exc}")
                continue

            if ids_or_sources:
                if debug:
                    print(f"  [discovery] {label}: {len(ids_or_sources)} hits")
                sources = self._hydrate(ids_or_sources)
                sources = [
                    s for s in sources
                    if s.get("type") in ("journal", None)
                    and (country is None or s.get("country_code") == country)
                ]
                if sources:
                    print(f"  (found via {label})")
                    return sources[:limit]
            elif debug:
                print(f"  [discovery] {label}: 0 hits")

        return []

    def _by_works_groupby(self, subject: str, limit: int, minw: int) -> list[str]:
        """Which journals actually publish this subject, recently?

        group_by returns at most 200 groups per call, so a large run needs
        several passes over different year slices — the same journals recur
        but the tail differs, which is where the smaller specialist titles
        live.
        """
        from datetime import date

        this_year = date.today().year
        y_from = this_year - 5

        # One pass for the whole window, then per-year passes when the caller
        # wants more than 200 groups' worth of candidates.
        windows = [f"publication_year:>{y_from}"]
        if limit > 60:
            windows += [
                f"publication_year:{y}" for y in range(this_year - 1, y_from, -1)
            ]

        on_topic: dict[str, int] = {}
        for window in windows:
            try:
                page = self._get(
                    "/works",
                    filter=f"title_and_abstract.search:{subject},"
                           f"type:article,{window},"
                           "primary_location.source.type:journal",
                    group_by="primary_location.source.id",
                    per_page=200,
                )
            except Exception:  # noqa: BLE001
                continue

            for g in page.get("group_by", []):
                key = g.get("key")
                if not key or key == "unknown":
                    continue
                sid = key.rsplit("/", 1)[-1]
                on_topic[sid] = max(on_topic.get(sid, 0), g.get("count", 0))

            if len(on_topic) >= limit * 6:
                break

        # A journal needs a real body of work on the subject, not a stray
        # mention. Three papers was far too low: three hits in a small
        # journal clears any percentage floor on what is essentially noise,
        # which is how a sustainability journal reached a web-services
        # shortlist.
        MIN_HITS = 8
        candidates = [k for k, v in on_topic.items() if v >= MIN_HITS]
        if len(candidates) < limit // 2:
            # Niche subject — relax, but never below 4.
            candidates = [k for k, v in on_topic.items() if v >= 4]
        if not candidates:
            return []

        scored = []
        for src in self._hydrate(candidates[: limit * 8]):
            sid = src["id"].rsplit("/", 1)[-1]
            hits = on_topic.get(sid, 0)
            recent = sum(
                c.get("works_count", 0)
                for c in src.get("counts_by_year", [])
                if c.get("year", 0) > y_from
            ) or src.get("works_count", 0)

            share = hits / max(recent, 1)

            # Raised from 1.5%. Below 3%, the subject is incidental to what
            # the journal publishes.
            if share < 0.03:
                continue

            # Absolute volume now counts for as much as concentration.
            # sqrt rather than ^0.3 stops a journal with a handful of hits
            # outranking one with twenty-five.
            relevance = share * (hits ** 0.5)
            scored.append((relevance, sid, hits, share))

        # Keep the relevance figures so the shortlist can rank on them too.
        self.last_relevance = {
            sid: {"on_topic_hits": h, "subject_share": sh}
            for _, sid, h, sh in scored
        }

        scored.sort(key=lambda t: -t[0])
        return [t[1] for t in scored[: limit * 2]]

    def _by_topic(self, subject: str, limit: int, minw: int) -> list[dict]:
        page = self._get("/topics", search=subject, per_page=1)
        results = page.get("results", [])
        if not results:
            return []
        tid = results[0]["id"].rsplit("/", 1)[-1]
        page = self._get(
            "/sources",
            filter=f"type:journal,topics.id:{tid},works_count:>{minw * 3}",
            sort="works_count:desc",
            per_page=min(200, limit),
        )
        return page.get("results", [])

    def _by_concept(self, subject: str, limit: int, minw: int) -> list[dict]:
        page = self._get("/concepts", search=subject, per_page=1)
        results = page.get("results", [])
        if not results:
            return []
        cid = results[0]["id"].rsplit("/", 1)[-1]
        page = self._get(
            "/sources",
            filter=f"type:journal,x_concepts.id:{cid},works_count:>{minw * 3}",
            sort="works_count:desc",
            per_page=min(200, limit),
        )
        return page.get("results", [])

    def _by_name_search(self, subject: str, limit: int, minw: int) -> list[dict]:
        """Last resort: journals whose title matches. Noisy but never empty."""
        page = self._get(
            "/sources",
            search=subject,
            filter=f"type:journal,works_count:>{minw * 3}",
            per_page=min(200, limit),
        )
        return page.get("results", [])

    def _hydrate(self, items: list) -> list[dict]:
        """Turn a list of ids into full source records; pass dicts through."""
        if not items:
            return []
        if isinstance(items[0], dict):
            return items

        out = []
        for i in range(0, len(items), 50):
            batch = "|".join(items[i : i + 50])
            page = self._get("/sources", filter=f"ids.openalex:{batch}", per_page=50)
            out.extend(page.get("results", []))
        return out

    def source_by_issn(self, issn: str) -> dict | None:
        try:
            return self._get(f"/sources/issn:{issn.strip()}")
        except requests.HTTPError:
            return None

    # ---------------- per-journal detail ----------------

    def research_output(self, source_id: str, years: list[int]) -> dict[int, int]:
        """Research articles and reviews per year, in ONE call.

        The previous version made one request per year, so a 60-journal run
        over a 4-year window cost 240 requests just for counts. group_by
        returns the whole series at once.
        """
        sid = source_id.rsplit("/", 1)[-1]
        lo, hi = min(years), max(years)
        page = self._get(
            "/works",
            filter=f"primary_location.source.id:{sid},"
                   f"publication_year:{lo}-{hi},type:article|review",
            group_by="publication_year",
            per_page=200,
        )
        counts = {y: 0 for y in years}
        for g in page.get("group_by", []):
            try:
                year = int(g["key"])
            except (KeyError, TypeError, ValueError):
                continue
            if year in counts:
                counts[year] = g.get("count", 0)
        return counts

    def composition_signals(self, source_id: str, year: int) -> dict:
        """Sample recent works for special-issue share and author mix.

        Two bugs fixed here. Issue numbers live under `biblio`, not
        `primary_location`, so the old code read an always-empty field and
        every journal scored 0% special issues. And the token list included
        the bare letter "s", which matched any issue string containing an s.
        """
        sid = source_id.rsplit("/", 1)[-1]
        page = self._get(
            "/works",
            filter=f"primary_location.source.id:{sid},publication_year:{year},"
                   "type:article|review",
            per_page=200,
            select="id,biblio,authorships",
        )
        works = page.get("results", [])
        if not works:
            return {}

        special = 0
        countries: dict[str, int] = {}
        multi_country = 0

        for w in works:
            issue = str((w.get("biblio") or {}).get("issue") or "").strip().lower()
            # Special issues are labelled like "S1", "3S", "Suppl 2", "spec".
            # A plain numeric issue is a regular issue.
            if issue and not issue.isdigit():
                if ("suppl" in issue or "spec" in issue
                        or re.fullmatch(r"s\d+|\d+s", issue)):
                    special += 1

            ctry = set()
            for a in w.get("authorships", []):
                for c in a.get("countries", []) or []:
                    ctry.add(c)
            for c in ctry:
                countries[c] = countries.get(c, 0) + 1
            if len(ctry) > 1:
                multi_country += 1

        n = len(works)
        return {
            "special_issue_share": special / n,
            "intl_author_share": multi_country / n,
            "top_country_share": max(countries.values()) / n if countries else None,
            "sample_size": n,
        }

    def build_record(
        self, source: dict, years: list[int], composition: bool = True
    ) -> JournalRecord:
        summary = source.get("summary_stats", {}) or {}
        rec = JournalRecord(
            openalex_id=source["id"],
            display_name=source.get("display_name", ""),
            issn_l=source.get("issn_l"),
            issns=source.get("issn") or [],
            publisher=source.get("host_organization_name"),
            is_oa=bool(source.get("is_oa")),
            apc_usd=source.get("apc_usd"),
            apc_prices=source.get("apc_prices") or [],
            is_in_doaj=bool(source.get("is_in_doaj")),
            homepage=source.get("homepage_url"),
            cited_by_count=source.get("cited_by_count", 0),
            h_index=summary.get("h_index"),
            two_yr_mean_citedness=summary.get("2yr_mean_citedness"),
        )
        rel = self.last_relevance.get(source["id"].rsplit("/", 1)[-1], {})
        rec.on_topic_hits = rel.get("on_topic_hits", 0)
        rec.subject_share = rel.get("subject_share")
        rec.works_by_year = {
            c["year"]: c["works_count"] for c in source.get("counts_by_year", [])
        }
        rec.research_works_by_year = self.research_output(rec.openalex_id, years)
        if composition:
            comp = self.composition_signals(rec.openalex_id, max(years))
            rec.special_issue_share = comp.get("special_issue_share")
            rec.intl_author_share = comp.get("intl_author_share")
            rec.top_country_share = comp.get("top_country_share")
        return rec


def iter_records(
    client: OpenAlexClient,
    sources: list[dict],
    years: list[int],
    composition: bool = True,
    progress: bool = True,
) -> Iterator[JournalRecord]:
    """Yield records, reporting progress and pace as it goes.

    A long run with no output is indistinguishable from a hung one, so this
    prints each journal with a counter and a running rate.
    """
    total = len(sources)
    start = time.time()

    for i, src in enumerate(sources, 1):
        name = str(src.get("display_name", "?"))[:44]
        if progress:
            elapsed = time.time() - start
            rate = elapsed / max(i - 1, 1)
            eta = rate * (total - i)
            eta_s = f" ~{eta / 60:.0f}m left" if i > 3 and eta > 60 else (
                f" ~{eta:.0f}s left" if i > 3 else ""
            )
            print(f"    [{i}/{total}] {name:<44}{eta_s}", flush=True)
        try:
            yield client.build_record(src, years, composition=composition)
        except Exception as exc:  # noqa: BLE001
            print(f"    ! skipped {name}: {type(exc).__name__}")
