"""How fast a journal gets a paper to acceptance — and how sure we are.

Two kinds of evidence, never blended:

    Measured  — median days from received to accepted across recent articles,
                from the dates publishers deposit with Crossref
                (turnaround.py). A fact about what the journal actually did.
    Stated    — the journal's own "submission to acceptance" or "time to first
                decision" figure, read from its homepage or metrics page
                (acceptance.py). Useful, but a claim.

Measured wins when both exist. Speed is reported as days plus a band, and the
source is always shown, so "fast" is never asserted without saying why.

A measured median under 14 days is not treated as a virtue: genuine peer review
rarely finishes that quickly, so it is banded "Suspiciously fast" and left out
of the fast-acceptance filter.
"""
from __future__ import annotations

import math
import re

BAND_SUSPICIOUS = "Suspiciously fast"
BAND_VERY_FAST = "Very fast"
BAND_FAST = "Fast"
BAND_MODERATE = "Moderate"
BAND_SLOW = "Slow"
BAND_UNKNOWN = "Unknown"

SUSPICIOUS_BELOW = 14
VERY_FAST_MAX = 45
FAST_MAX = 90
MODERATE_MAX = 180

# "Submission to acceptance: 38 days", "time to first decision 2.5 weeks",
# "Acceptance to publication 4 days" — publisher metrics pages (MDPI, Springer,
# Elsevier Journal Insights, Frontiers, Hindawi) phrase it roughly this way.
_UNIT = r"(\d{1,3}(?:\.\d{1,2})?)\s*(days?|weeks?|wks?|months?)"
STATED_ACCEPT = re.compile(
    r"(?:submission|received|submit(?:ted)?)\s*(?:to|-|–|→)\s*(?:final\s+)?acceptance"
    r"[^0-9]{0,40}?" + _UNIT, re.I)
STATED_FIRST_DECISION = re.compile(
    r"(?:(?:submission|received)\s*(?:to|-|–|→)\s*)?first\s+decision"
    r"[^0-9]{0,40}?" + _UNIT, re.I)
STATED_ACCEPT_TO_PUB = re.compile(
    r"acceptance\s*(?:to|-|–|→)\s*(?:online\s+)?publication[^0-9]{0,40}?" + _UNIT, re.I)


def _days(num: str, unit: str) -> float | None:
    try:
        v = float(num)
    except ValueError:
        return None
    u = unit.lower()
    if u.startswith("w"):
        v *= 7
    elif u.startswith("m"):
        v *= 30.4
    return v if 0 < v <= 730 else None


def parse_stated_speed(text: str) -> dict:
    """Stated durations from page text, in days."""
    out = {}
    for key, pat in (("stated_days_to_acceptance", STATED_ACCEPT),
                     ("stated_days_to_first_decision", STATED_FIRST_DECISION),
                     ("stated_days_accept_to_publication", STATED_ACCEPT_TO_PUB)):
        m = pat.search(text or "")
        if m:
            d = _days(m.group(1), m.group(2))
            if d is not None:
                out[key] = round(d, 1)
    return out


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def band(days: float | None, measured: bool) -> str:
    if days is None:
        return BAND_UNKNOWN
    if measured and days < SUSPICIOUS_BELOW:
        return BAND_SUSPICIOUS
    if days <= VERY_FAST_MAX:
        return BAND_VERY_FAST
    if days <= FAST_MAX:
        return BAND_FAST
    if days <= MODERATE_MAX:
        return BAND_MODERATE
    return BAND_SLOW


def classify(row: dict) -> dict:
    """Speed columns for one journal row."""
    measured = _num(row.get("review_days_median"))
    stated_acc = _num(row.get("stated_days_to_acceptance"))
    stated_fd = _num(row.get("stated_days_to_first_decision")) \
        or _num(row.get("days_to_first_decision"))
    production = _num(row.get("production_days")) \
        or _num(row.get("stated_days_accept_to_publication"))

    if measured is not None:
        days, src, is_measured = measured, "Measured (Crossref dates)", True
    elif stated_acc is not None:
        days, src, is_measured = stated_acc, "Stated by journal", False
    else:
        days, src, is_measured = None, None, False

    out = {
        "acceptance_days": round(days) if days is not None else None,
        "acceptance_speed": band(days, is_measured),
        "speed_source": src,
        "first_decision_days": round(stated_fd) if stated_fd is not None else None,
        "days_to_publication": (round(days + production)
                                if days is not None and production is not None else None),
    }
    # a journal with only a first-decision figure: show it, but do not call it
    # fast acceptance — a quick rejection is also a first decision
    if days is None and stated_fd is not None:
        out["speed_source"] = "Stated first decision only"
    return out


def display(row: dict) -> str:
    d, b = _num(row.get("acceptance_days")), row.get("acceptance_speed")
    if d is None:
        fd = _num(row.get("first_decision_days"))
        return f"1st decision ~{fd:.0f} d" if fd is not None else "Unknown"
    return f"~{d:.0f} d · {b}"


def is_fast(row: dict, max_days: int) -> bool:
    d = _num(row.get("acceptance_days"))
    return (d is not None and d <= max_days
            and row.get("acceptance_speed") != BAND_SUSPICIOUS)
