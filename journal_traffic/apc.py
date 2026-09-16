"""Article processing charges (APCs) for every shortlisted journal.

A client's first question after "will it get in?" is "what will it cost?", and
OpenAlex's ``apc_usd`` alone leaves most rows blank. So the charge is looked up
from several sources, most authoritative first, and every figure carries where
it came from:

    1. DOAJ           — the journal's own declaration to the Directory of Open
                        Access Journals (free API, no key). Also the only source
                        that can say "no APC" with authority (diamond OA).
    2. OpenAlex       — ``apc_prices`` / ``apc_usd`` on the source record, which
                        OpenAlex compiles from DOAJ and publisher price lists.
    3. Publisher page — the journal's homepage and the publisher's usual APC
                        subpage, read for "APC … USD 2,390"-style statements.
                        Lowest confidence, so it only fills gaps.

Status is reported separately from the amount, because "blank" means different
things: a subscription journal has no mandatory APC (an optional hybrid OA fee
may apply), a diamond OA journal charges nothing, and "unknown" is unknown.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import date
from hashlib import sha1
from pathlib import Path

import requests

DOAJ_API = "https://doaj.org/api/search/journals/"
CACHE_DIR = Path("cache") / "apc"
CACHE_TTL_SECONDS = 14 * 24 * 3600

# Approximate conversion to USD, used only when a live rate is unavailable.
# Shown to the user as approximate; the original currency is always kept.
FALLBACK_USD_RATES = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CHF": 1.12, "JPY": 0.0066,
    "CNY": 0.14, "INR": 0.012, "AUD": 0.66, "CAD": 0.73, "BRL": 0.18,
    "KRW": 0.00073, "SGD": 0.74, "HKD": 0.13, "PLN": 0.25, "CZK": 0.043,
    "SEK": 0.095, "NOK": 0.093, "DKK": 0.145, "TRY": 0.03, "ZAR": 0.054,
    "MYR": 0.21, "IDR": 0.000062, "RUB": 0.011, "MXN": 0.055, "NZD": 0.60,
    "PKR": 0.0036, "EGP": 0.02, "IRR": 0.000024, "UAH": 0.024, "RON": 0.22,
}

STATUS_CHARGES = "Charges APC"
STATUS_NO_APC = "No APC"
STATUS_SUBSCRIPTION = "Subscription (hybrid OA optional)"
STATUS_UNKNOWN = "Unknown"

# Publishing model — what kind of journal it is
MODEL_DIAMOND = "Diamond OA"        # open access, no charge to anyone
MODEL_GOLD = "Gold OA"              # fully open access, APC to publish
MODEL_HYBRID = "Hybrid"             # subscription journal, OA is an optional paid extra
MODEL_SUBSCRIPTION = "Subscription"
MODEL_UNKNOWN = "Unknown"

# Can the author publish without paying?
FREE_YES = "Yes"
FREE_WAIVER = "Waiver on request"
FREE_NO = "No"
FREE_UNKNOWN = "Unknown"


@dataclass
class APCInfo:
    apc_status: str = STATUS_UNKNOWN
    apc_amount: float | None = None
    apc_currency: str | None = None
    apc_usd_est: int | None = None
    apc_source: str | None = None
    apc_url: str | None = None
    apc_note: str | None = None
    apc_checked: str | None = None
    publishing_model: str = MODEL_UNKNOWN
    free_option: str = FREE_UNKNOWN
    free_option_note: str | None = None
    # internal hints used while classifying (kept in the CSV — harmless)
    waiver: bool = False
    page_says_hybrid: bool = False

    def as_row(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# currency
# --------------------------------------------------------------------------- #
_LIVE_RATES: dict[str, float] | None = None


def usd_rates() -> dict[str, float]:
    """Units of USD per unit of currency. Live ECB rates when reachable."""
    global _LIVE_RATES
    if _LIVE_RATES is not None:
        return _LIVE_RATES
    rates = dict(FALLBACK_USD_RATES)
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD", timeout=6)
        if r.ok:
            for cur, per_usd in (r.json().get("rates") or {}).items():
                if per_usd:
                    rates[cur.upper()] = 1.0 / float(per_usd)
    except (requests.RequestException, ValueError):
        pass
    _LIVE_RATES = rates
    return rates


def to_usd(amount: float | None, currency: str | None) -> int | None:
    if amount is None or not currency:
        return None
    rate = usd_rates().get(currency.upper())
    return int(round(amount * rate)) if rate else None


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def _cache_get(key: str):
    f = CACHE_DIR / (sha1(key.encode()).hexdigest()[:20] + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL_SECONDS:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _cache_put(key: str, value) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = CACHE_DIR / (sha1(key.encode()).hexdigest()[:20] + ".json")
        f.write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 1. DOAJ
# --------------------------------------------------------------------------- #
def _doaj_record(issns: list[str]) -> dict | None:
    for issn in issns:
        issn = (issn or "").strip()
        if not re.match(r"^\d{4}-\d{3}[\dXx]$", issn):
            continue
        key = f"doaj:{issn}"
        cached = _cache_get(key)
        if cached is not None:
            if cached:
                return cached
            continue
        try:
            r = requests.get(DOAJ_API + f"issn:{issn}", timeout=10,
                             headers={"Accept": "application/json"})
            data = r.json() if r.ok else {}
        except (requests.RequestException, ValueError):
            continue                      # network trouble: do not cache a miss
        results = data.get("results") or []
        rec = results[0] if results else {}
        _cache_put(key, rec)
        if rec:
            return rec
    return None


def from_doaj(issns: list[str]) -> APCInfo | None:
    rec = _doaj_record(issns)
    if not rec:
        return None
    bib = rec.get("bibjson") or {}
    apc = bib.get("apc") or {}
    url = apc.get("url") or (bib.get("ref") or {}).get("journal")
    has = apc.get("has_apc")
    waiver = bool((bib.get("waiver") or {}).get("has_waiver"))
    info = _doaj_info(apc, url, has)
    if info is not None:
        info.waiver = waiver
        info.publishing_model = MODEL_DIAMOND if has is False else MODEL_GOLD
    return info


def _doaj_info(apc: dict, url, has) -> APCInfo | None:
    if has is False:
        return APCInfo(apc_status=STATUS_NO_APC, apc_amount=0, apc_currency=None,
                       apc_usd_est=0, apc_source="DOAJ", apc_url=url,
                       apc_note="Declared to DOAJ: no article processing charge")
    prices = [p for p in (apc.get("max") or []) if p.get("price") is not None]
    if has and prices:
        # prefer a USD price when the journal lists several currencies
        best = next((p for p in prices if (p.get("currency") or "").upper() == "USD"),
                    prices[0])
        amt, cur = float(best["price"]), (best.get("currency") or "").upper()
        return APCInfo(apc_status=STATUS_CHARGES, apc_amount=amt, apc_currency=cur,
                       apc_usd_est=to_usd(amt, cur), apc_source="DOAJ", apc_url=url,
                       apc_note=("Also listed: " + ", ".join(
                           f"{p['price']} {p.get('currency', '')}" for p in prices
                           if p is not best)) if len(prices) > 1 else None)
    if has:
        return APCInfo(apc_status=STATUS_CHARGES, apc_source="DOAJ", apc_url=url,
                       apc_note="DOAJ says an APC is charged but gives no amount")
    return None


# --------------------------------------------------------------------------- #
# 2. OpenAlex
# --------------------------------------------------------------------------- #
def from_openalex(row: dict) -> APCInfo | None:
    prices = [p for p in (row.get("apc_prices") or []) if p.get("price") is not None]
    usd = row.get("apc_usd")
    if prices:
        best = next((p for p in prices if (p.get("currency") or "").upper() == "USD"),
                    prices[0])
        amt, cur = float(best["price"]), (best.get("currency") or "").upper()
        if amt == 0:
            return APCInfo(apc_status=STATUS_NO_APC, apc_amount=0, apc_usd_est=0,
                           apc_source="OpenAlex")
        return APCInfo(apc_status=STATUS_CHARGES, apc_amount=amt, apc_currency=cur,
                       apc_usd_est=int(usd) if usd else to_usd(amt, cur),
                       apc_source="OpenAlex")
    if usd:
        return APCInfo(apc_status=STATUS_CHARGES, apc_amount=float(usd),
                       apc_currency="USD", apc_usd_est=int(usd), apc_source="OpenAlex")
    return None


# --------------------------------------------------------------------------- #
# 3. Publisher page
# --------------------------------------------------------------------------- #
_CUR = r"(USD|US\$|EUR|GBP|CHF|INR|CNY|JPY|AUD|CAD|\$|€|£|₹|¥)"
_NUM = r"(\d{1,3}(?:[,.\s]\d{3})+|\d{2,5})(?:\.\d{2})?"
_APC_WORDS = (r"(?:article\s+processing\s+charges?|article\s+publishing\s+charges?|"
              r"publication\s+(?:fee|charge)s?|open\s+access\s+(?:fee|charge|price)s?|"
              r"\bAPCs?\b)")
APC_PATTERNS = [
    re.compile(_APC_WORDS + r"[^.]{0,80}?" + _CUR + r"\s?" + _NUM, re.I),
    re.compile(_APC_WORDS + r"[^.]{0,80}?" + _NUM + r"\s?" + _CUR, re.I),
    re.compile(_CUR + r"\s?" + _NUM + r"[^.]{0,60}?" + _APC_WORDS, re.I),
]
NO_APC_PATTERN = re.compile(
    r"(?:\bno|free\s+of|without|does\s+not\s+charge(?:\s+any)?|waives?\s+all)\s+"
    r"(?:article\s+processing\s+charges?|APCs?|publication\s+fees?|author\s+fees?)"
    r"|(?:APC|article\s+processing\s+charge)s?\s*(?:is|are|:)?\s*(?:free|waived|none|nil|0)\b",
    re.I)
# wording that means open access is optional and the normal route is free
HYBRID_PATTERN = re.compile(
    r"\bhybrid\b|subscription[- ]based|traditional\s+(?:subscription|publishing)|"
    r"(?:option|choose|opt)\s+(?:to\s+)?(?:publish\s+)?(?:your\s+(?:article|paper)\s+)?"
    r"(?:open\s+access|OA)|open\s+access\s+(?:is\s+)?optional|"
    r"no\s+(?:charge|fee)\s+(?:to|for)\s+publish(?:ing)?\s+(?:under|via|through)\s+the\s+subscription",
    re.I)
_SYMBOL = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}

# where each publisher states its APC, relative to the journal homepage
APC_SUBPAGES = {
    "springer": ["/how-to-publish-with-us", "/fees-and-funding"],
    "biomedcentral": ["/submission-guidelines/fees-and-funding"],
    "sciencedirect": ["/publish/open-access-options"],
    "elsevier": ["/open-access-options"],
    "mdpi": ["/apc"],
    "tandfonline": ["/journal-information"],
    "wiley": ["/journal-overview", "/about"],
    "sagepub": ["/description"],
    "frontiersin": ["/about#about-fees"],
    "hindawi": ["/apc"],
    "plos": ["/s/publication-fees"],
    "ieee": ["/about-journal"],
    "iopscience": ["/page/Open%20access"],
    "emerald": ["/author-guidelines"],
}


def _amount(raw: str) -> float | None:
    s = re.sub(r"[,\s]", "", raw)
    if s.count(".") == 1 and len(s.split(".")[1]) == 3:     # 2.390 = 2390
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 50 <= v <= 20000 else None


def parse_apc_text(text: str) -> tuple[float | None, str | None, bool]:
    """(amount, currency, says_no_apc) from page text."""
    for pat in APC_PATTERNS:
        for m in pat.finditer(text):
            groups = [g for g in m.groups() if g]
            cur = next((g for g in groups if re.match(_CUR + "$", g, re.I)), None)
            num = next((g for g in groups if re.match(r"^[\d,.\s]+$", g)), None)
            amt = _amount(num) if num else None
            if amt and cur:
                cur = _SYMBOL.get(cur, cur.upper().replace("US$", "USD"))
                return amt, cur, False
    return None, None, bool(NO_APC_PATTERN.search(text))


def apc_urls(homepage: str | None) -> list[str]:
    if not homepage:
        return []
    base = homepage.rstrip("/")
    urls = [base]
    low = base.lower()
    for key, paths in APC_SUBPAGES.items():
        if key in low:
            urls = [base + p for p in paths] + urls
            break
    return urls


def from_publisher_page(homepage: str | None) -> APCInfo | None:
    from .acceptance import _fetch          # shares the browser headers + stats
    said_free_at = None
    for url in apc_urls(homepage):
        cached = _cache_get(f"page:{url}")
        if cached is None:
            html = _fetch(url)
            if html is None:
                continue
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
            amt, cur, free = parse_apc_text(text)
            cached = {"amt": amt, "cur": cur, "free": free,
                      "hybrid": bool(HYBRID_PATTERN.search(text))}
            _cache_put(f"page:{url}", cached)
        if cached.get("amt"):
            amt, cur = cached["amt"], cached["cur"]
            return APCInfo(apc_status=STATUS_CHARGES, apc_amount=amt, apc_currency=cur,
                           apc_usd_est=to_usd(amt, cur), apc_source="Publisher page",
                           apc_url=url, apc_note="Read from the publisher's page — verify",
                           page_says_hybrid=bool(cached.get("hybrid")))
        if cached.get("free") and said_free_at is None:
            said_free_at = url
    if said_free_at:
        return APCInfo(apc_status=STATUS_NO_APC, apc_amount=0, apc_usd_est=0,
                       apc_source="Publisher page", apc_url=said_free_at,
                       apc_note="Page states no APC — verify")
    return None


# --------------------------------------------------------------------------- #
# combine
# --------------------------------------------------------------------------- #
def lookup(row: dict, scrape: bool = True) -> APCInfo:
    """Best available APC for one journal row (a JournalRecord as dict)."""
    issns = list(row.get("issns") or [])
    if row.get("issn_l") and row["issn_l"] not in issns:
        issns.insert(0, row["issn_l"])
    today = date.today().isoformat()

    info = None
    try:
        info = from_doaj(issns)
    except Exception:  # noqa: BLE001 — one bad source must not sink the rest
        info = None
    if info is None or (info.apc_status == STATUS_CHARGES and info.apc_amount is None):
        oa = from_openalex(row)
        if oa is not None:
            if info is not None:            # keep DOAJ's link/model, take the amount
                oa.apc_url = info.apc_url
                oa.apc_source = "DOAJ + OpenAlex"
                oa.waiver = info.waiver
                oa.publishing_model = info.publishing_model
            info = oa
    if (info is None or info.apc_amount is None) and scrape:
        try:
            page = from_publisher_page(row.get("homepage"))
        except Exception:  # noqa: BLE001
            page = None
        if page is not None and (info is None or page.apc_amount is not None):
            info = page

    if info is None:
        if row.get("is_oa") or row.get("is_in_doaj"):
            info = APCInfo(apc_status=STATUS_UNKNOWN,
                           apc_note="Open access journal; charge not published "
                                    "in DOAJ, OpenAlex or on its homepage")
        else:
            info = APCInfo(apc_status=STATUS_SUBSCRIPTION,
                           apc_note="No mandatory APC to publish; an optional "
                                    "open-access fee may apply")
        info.apc_url = row.get("homepage")
    info.apc_checked = today
    classify(info, row)
    return info


def classify(info: APCInfo, row: dict) -> APCInfo:
    """Work out the publishing model and whether a free route exists.

    The key trap: OpenAlex lists a price for many *hybrid* journals. Shown as a
    bare "USD 3,290" it reads as a fee the author must pay, when the author can
    publish in that journal for free through the normal subscription route and
    only pays if they want open access.
    """
    fully_oa = bool(row.get("is_oa") or row.get("is_in_doaj")) \
        or info.apc_source in ("DOAJ", "DOAJ + OpenAlex")

    if info.publishing_model == MODEL_UNKNOWN:
        if info.apc_status == STATUS_NO_APC:
            info.publishing_model = MODEL_DIAMOND if fully_oa or \
                info.apc_source == "DOAJ" else MODEL_UNKNOWN
            if info.publishing_model == MODEL_UNKNOWN:
                info.publishing_model = MODEL_DIAMOND
        elif info.apc_status == STATUS_SUBSCRIPTION:
            info.publishing_model = MODEL_SUBSCRIPTION
        elif info.apc_status == STATUS_CHARGES:
            if fully_oa and not info.page_says_hybrid:
                info.publishing_model = MODEL_GOLD
            else:
                info.publishing_model = MODEL_HYBRID
        elif fully_oa:
            info.publishing_model = MODEL_GOLD

    m = info.publishing_model
    if m == MODEL_DIAMOND:
        info.free_option = FREE_YES
        info.free_option_note = "Open access with no charge"
    elif m in (MODEL_HYBRID, MODEL_SUBSCRIPTION):
        info.free_option = FREE_YES
        info.free_option_note = (
            "Free via the standard subscription route"
            + (f"; open access is optional at {display(info.as_row(), short=True)}"
               if info.apc_amount else "; optional open-access fee may apply"))
        if info.apc_status == STATUS_CHARGES:
            info.apc_status = STATUS_CHARGES + " (optional)"
    elif m == MODEL_GOLD:
        if info.waiver:
            info.free_option = FREE_WAIVER
            info.free_option_note = ("APC required, but the journal offers waivers "
                                     "(e.g. for low-income countries) — request at "
                                     "submission")
        else:
            info.free_option = FREE_NO
            info.free_option_note = "Fully open access — the APC must be paid"
    else:
        info.free_option = FREE_UNKNOWN
        info.free_option_note = "Could not confirm whether a free route exists"
    return info


def lookup_many(rows: dict[str, dict], scrape: bool = True, workers: int = 10,
                progress: bool = True) -> dict[str, APCInfo]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: dict[str, APCInfo] = {}
    if not rows:
        return out
    usd_rates()                              # warm once, not per thread
    total, done = len(rows), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(lookup, r, scrape): k for k, r in rows.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                out[k] = fut.result()
            except Exception:  # noqa: BLE001
                out[k] = APCInfo(apc_checked=date.today().isoformat())
            done += 1
            if progress and (done % 10 == 0 or done == total):
                priced = sum(1 for v in out.values() if v.apc_amount is not None)
                print(f"    APC checked {done}/{total}, {priced} with an amount",
                      flush=True)
    return out


def summary(infos: dict[str, APCInfo]) -> str:
    by = {}
    for v in infos.values():
        by[v.apc_status] = by.get(v.apc_status, 0) + 1
    src = {}
    for v in infos.values():
        if v.apc_source:
            src[v.apc_source] = src.get(v.apc_source, 0) + 1
    return ("APC status: " + ", ".join(f"{k} {n}" for k, n in sorted(by.items())) +
            ("  |  sources: " + ", ".join(f"{k} {n}" for k, n in src.items())
             if src else ""))


def display(info_row: dict, short: bool = False) -> str:
    """Compact cell text, e.g. 'USD 2,390', 'EUR 1,800 (~$1,944)', 'No APC',
    'USD 3,290 optional · free route'."""
    st = info_row.get("apc_status")
    if not short and isinstance(st, str) and st.endswith("(optional)"):
        base = display({**info_row, "apc_status": STATUS_CHARGES}, short=True)
        return f"{base} optional · free route"
    if not short and info_row.get("publishing_model") == MODEL_SUBSCRIPTION:
        return "Free (subscription route)"
    amt, cur, usd = (info_row.get("apc_amount"), info_row.get("apc_currency"),
                     info_row.get("apc_usd_est"))
    try:
        amt = None if amt is None or amt != amt else float(amt)
    except (TypeError, ValueError):
        amt = None
    if st == STATUS_NO_APC:
        return "No APC (free)"
    if amt:
        txt = f"{cur or ''} {amt:,.0f}".strip()
        try:
            if cur and cur != "USD" and usd == usd and usd:
                txt += f" (~${int(usd):,})"
        except (TypeError, ValueError):
            pass
        return txt
    if st == STATUS_SUBSCRIPTION:
        return "Subscription"
    if st == STATUS_CHARGES:
        return "APC (amount not listed)"
    return "Unknown"
