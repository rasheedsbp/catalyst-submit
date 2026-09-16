"""Lag correction — converting a backward-looking estimate into a current one.

The problem
-----------
`estimate_traffic` computes:

    submissions(year Y) ≈ articles_published(Y) / acceptance_rate

But the articles published in year Y were submitted in year Y minus the
pipeline lag. So the number is correctly labelled as traffic from roughly a
year ago, not now. For a flat journal that hardly matters. For one growing 25%
a year it understates current inflow by about a quarter.

The correction
--------------
Two steps.

1. Attribute published output to its submission cohort by shifting back by the
   pipeline lag L (in years). Articles published in 2025 with an 14-month lag
   are mostly a 2024 submission cohort.

2. Project that cohort forward to today using the journal's observed growth
   rate, compounding over L.

        submissions_now ≈ submissions(cohort year) * (1 + g) ** L

Both steps widen the error bars, and this module widens them explicitly rather
than quietly. A projection is a weaker claim than a measurement and the output
says so.

Where L comes from, best first:
  - median online-to-print lag observed in Crossref (realtime module)
  - the journal's declared time-to-first-decision, times a review-round factor
  - a publisher-family default
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Fallback pipeline lag in months, submission to publication, by publisher.
# Only used when nothing observed is available.
PUBLISHER_LAG_MONTHS = {
    "elsevier": 11.0,
    "springer": 13.0,
    "wiley": 13.0,
    "ieee": 10.0,
    "mdpi": 3.0,        # notably fast; distorts comparisons if left uncorrected
    "taylor": 14.0,
    "sage": 13.0,
    "emerald": 12.0,
    "hindawi": 5.0,
    "frontiers": 5.0,
}
DEFAULT_LAG_MONTHS = 12.0

# First-decision time is only part of the pipeline. Multiply by this to get
# total submission-to-publication lag: revision rounds plus production.
DECISION_TO_PUBLICATION_FACTOR = 3.2


def infer_lag_months(row: pd.Series) -> tuple[float, str]:
    """Best available pipeline lag, with its provenance."""
    observed = row.get("median_lag_days")
    if pd.notna(observed) and 30 <= float(observed) <= 900:
        # Online-to-print understates total lag; add the pre-acceptance portion.
        return float(observed) / 30.4 + 6.0, "observed"

    decision = row.get("days_to_first_decision")
    if pd.notna(decision) and 3 <= float(decision) <= 365:
        return float(decision) / 30.4 * DECISION_TO_PUBLICATION_FACTOR, "declared"

    pub = str(row.get("publisher") or "").lower()
    for key, months in PUBLISHER_LAG_MONTHS.items():
        if key in pub:
            return months, "publisher_default"

    return DEFAULT_LAG_MONTHS, "global_default"


def annual_growth_rate(counts: dict) -> float:
    """CAGR of research output, clipped to a defensible range."""
    if not counts:
        return 0.0
    items = sorted((int(y), v) for y, v in counts.items() if v)
    if len(items) < 2:
        return 0.0
    (y0, v0), (y1, v1) = items[0], items[-1]
    span = y1 - y0
    if span <= 0 or v0 <= 0:
        return 0.0
    cagr = (v1 / v0) ** (1 / span) - 1
    # A journal cannot plausibly compound at more than ±40% for long, and
    # extrapolating an outlier year is how you get an absurd projection.
    return float(np.clip(cagr, -0.40, 0.40))


def apply_lag_correction(df: pd.DataFrame, reference_year: int) -> pd.DataFrame:
    """Add cohort attribution and a present-day projection.

    Adds:
      pipeline_lag_months, lag_source, growth_rate,
      submission_cohort_year, submissions_now_est,
      submissions_now_low, submissions_now_high, projection_confidence
    """
    out = df.copy()

    lags, sources, growths = [], [], []
    for _, row in out.iterrows():
        months, src = infer_lag_months(row)
        lags.append(months)
        sources.append(src)
        growths.append(annual_growth_rate(row.get("research_works_by_year") or {}))

    out["pipeline_lag_months"] = np.round(lags, 1)
    out["lag_source"] = sources
    out["growth_rate"] = np.round(growths, 3)

    lag_years = out["pipeline_lag_months"] / 12.0
    out["submission_cohort_year"] = np.round(reference_year - lag_years, 1)

    # Years of projection needed to bring the cohort up to today.
    today_year = reference_year + 1  # reference_year is the last complete year
    horizon = (today_year - out["submission_cohort_year"]).clip(lower=0)

    factor = (1 + out["growth_rate"]) ** horizon

    base = out["submissions_est"].astype("float")
    lo = out["submissions_low"].astype("float")
    hi = out["submissions_high"].astype("float")

    # Projection uncertainty compounds with horizon and with how much we are
    # trusting an inferred lag. Widen the band accordingly.
    lag_penalty = out["lag_source"].map(
        {"observed": 0.05, "declared": 0.10,
         "publisher_default": 0.18, "global_default": 0.25}
    ).fillna(0.25)
    widen = 1 + lag_penalty * horizon

    out["submissions_now_est"] = (base * factor).round()
    out["submissions_now_low"] = (lo * factor / widen).round()
    out["submissions_now_high"] = (hi * factor * widen).round()

    # Confidence downgrades one step relative to the underlying evidence tier
    # whenever the lag is a guess or the horizon is long.
    conf = []
    for _, r in out.iterrows():
        tier = r.get("evidence_tier", "-")
        if r["lag_source"] in ("observed", "declared") and tier in ("A", "B"):
            conf.append("high")
        elif tier in ("A", "B") or r["lag_source"] == "observed":
            conf.append("moderate")
        elif tier == "C":
            conf.append("low")
        else:
            conf.append("indicative")
    out["projection_confidence"] = conf

    return out


def reconcile_with_realtime(
    df: pd.DataFrame, snapshots: dict[str, "object"]
) -> pd.DataFrame:
    """Cross-check the projection against observed acceptance throughput.

    If Crossref says a journal is currently accepting at a rate implying far
    more or less output than the projection assumes, that disagreement is worth
    surfacing — it usually means the growth rate or the lag is wrong.
    """
    out = df.copy()
    obs_annual, divergence = [], []

    for _, row in out.iterrows():
        issn = row.get("issn_l") or (row.get("issns") or [None])[0]
        snap = snapshots.get(str(issn)) if issn else None
        if snap is None:
            obs_annual.append(np.nan)
            divergence.append(np.nan)
            continue

        implied = snap.implied_annual_output()
        obs_annual.append(round(implied))

        expected = row.get("articles_latest", np.nan)
        if expected and not pd.isna(expected) and expected > 0:
            divergence.append(round(implied / expected - 1, 2))
        else:
            divergence.append(np.nan)

    out["observed_annual_output"] = obs_annual
    out["output_divergence"] = divergence
    out["realtime_flag"] = np.where(
        pd.Series(divergence).abs() > 0.30,
        "CHECK: live output diverges >30% from published-year baseline",
        "",
    )
    return out
