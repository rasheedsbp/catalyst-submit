"""Shortlist journals: best acceptance odds within a quality floor.

The naive version of this feature is dangerous. Rank purely by "highest
acceptance rate, lowest competition" and the top of the list is predatory
journals and paper mills — they accept nearly everything and nobody competes
to get in. Placing a client's work there costs them more than a rejection.

So the score has two parts. An opportunity score (how likely is acceptance,
how fast, how much room is there) and a set of hard quality guards that
exclude a journal regardless of how good its odds look.

The guards are deliberately conservative. Better to omit a legitimate journal
than to recommend a bad one to a paying client.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .publishers import add_publisher_column, enforce_diversity

# Warning signs. None is proof of anything on its own — several together mean
# the journal should not reach a shortlist without a human looking at it.
SUSPICIOUS_OUTPUT = 3000        # articles/year: megajournal or mill territory
EXTREME_GROWTH = 0.60           # 60%+ annual growth in output
HIGH_SPECIAL_ISSUE = 0.55       # majority of output via special issues
FAST_DECISION_DAYS = 10         # real peer review rarely finishes this fast
IMPLAUSIBLE_ACCEPTANCE = 0.70   # accepting 70%+ of submissions


def quality_flags(row: pd.Series) -> list[str]:
    """Reasons to look harder at a journal before recommending it."""
    flags = []

    if row.get("scopus_status") == "DISCONTINUED":
        flags.append("delisted from Scopus")
    elif row.get("scopus_status") == "INACTIVE":
        flags.append("Scopus coverage stopped")
    elif row.get("scopus_status") == "unknown":
        flags.append("Scopus coverage unconfirmed")

    arts = row.get("articles_latest") or 0
    if arts > SUSPICIOUS_OUTPUT:
        flags.append(f"very high output ({int(arts)}/yr)")

    growth = row.get("growth_rate")
    if pd.notna(growth) and growth > EXTREME_GROWTH:
        flags.append(f"output growing {growth:.0%}/yr")

    si = row.get("special_issue_share")
    if pd.notna(si) and si > HIGH_SPECIAL_ISSUE:
        flags.append(f"{si:.0%} special-issue output")

    days = row.get("days_to_first_decision")
    if pd.notna(days) and 0 < days < FAST_DECISION_DAYS:
        flags.append(f"first decision in {int(days)} days")

    # Measured review time under two weeks means there was no real peer
    # review, whatever the journal claims.
    measured = row.get("review_days_median")
    if pd.notna(measured) and 0 < measured < 14:
        flags.append(f"measured review only {int(measured)} days")

    rate = row.get("acceptance_rate_used")
    if pd.notna(rate) and rate > IMPLAUSIBLE_ACCEPTANCE:
        flags.append(f"accepts {rate:.0%}")

    hits = row.get("on_topic_hits")
    if pd.notna(hits) and 0 < hits < 10:
        flags.append(f"only {int(hits)} papers on this subject")

    sem = row.get("semantic_relevance")
    if pd.notna(sem) and sem < 0.25:
        flags.append(f"weak subject match ({sem:.2f})")

    apc = row.get("apc_usd")
    if pd.notna(apc) and apc and apc > 2500 and row.get("quartile") in (3, 4):
        flags.append(f"high APC (${int(apc)}) for a Q{int(row['quartile'])}")

    return flags


def opportunity_score(df: pd.DataFrame) -> pd.Series:
    """Composite 0-100 score. Higher means better odds, not better journal.

    Five components, weighted:
      relevance       (30%)  — how much of the journal's output is on this
                               subject. Without this, the score rewarded small
                               obscure journals: high acceptance odds and low
                               submission counts put a sustainability journal
                               top of a web-services shortlist.
      acceptance odds (25%)  — the regular-track odds, or the rate if absent
      review speed    (20%)  — MEASURED days from received to accepted
      capacity        (10%)  — is the journal expanding? more slots opening
      contention      (10%)  — fewer submissions chasing each slot
      predictability   (5%)  — tight IQR on review time; a journal that takes
                               90 days give or take 20 is far more usable to
                               a client on a deadline than one averaging 90
                               with a spread of 300

    Speed carries real weight now because it is measured from Crossref
    deposit dates rather than taken from a journal's own marketing claim.
    Where turnaround is unknown the component falls back to the declared
    figure, and failing that scores neutral rather than penalising.
    """
    def pct(series, invert=False):
        # A missing column arrives as None, and pd.to_numeric on a scalar
        # returns a scalar — coerce to a Series aligned on df either way.
        if series is None:
            return pd.Series(0.5, index=df.index)
        if not isinstance(series, pd.Series):
            series = pd.Series(series, index=df.index)
        s = pd.to_numeric(series, errors="coerce")
        if not isinstance(s, pd.Series):
            s = pd.Series(s, index=df.index)
        if s.notna().sum() < 2:
            return pd.Series(0.5, index=df.index)
        r = s.rank(pct=True)
        r = 1 - r if invert else r
        return r.fillna(0.4)  # unknown scores slightly below median

    # Use .get so a partially-built frame degrades to a neutral score
    # instead of raising KeyError — callers assemble these columns in
    # different orders.
    odds = df.get("regular_track_odds")
    if odds is None or not isinstance(odds, pd.Series):
        odds = pd.Series(np.nan, index=df.index)
    fallback = df.get("acceptance_rate_used")
    if isinstance(fallback, pd.Series):
        odds = odds.where(odds.notna(), fallback)

    # Measured review time first; fall back to the declared figure.
    speed = df.get("review_days_median")
    if speed is None or not isinstance(speed, pd.Series):
        speed = pd.Series(np.nan, index=df.index)
    speed = pd.to_numeric(speed, errors="coerce")

    declared = df.get("days_to_first_decision")
    if declared is not None and isinstance(declared, pd.Series):
        declared = pd.to_numeric(declared, errors="coerce")
        # Declared is time-to-FIRST-DECISION, not to acceptance; scale it so
        # the two are roughly comparable before filling gaps.
        speed = speed.where(speed.notna(), declared * 2.2)

    # Relevance. Prefer the semantic score — it compares the paper against
    # what the journal actually published, so it separates "web service QoS"
    # from "service quality in public transport", which a keyword count
    # cannot. Fall back to the keyword share only when profiling was skipped.
    relevance = df.get("semantic_relevance")
    if relevance is None or not isinstance(relevance, pd.Series) \
            or pd.to_numeric(relevance, errors="coerce").notna().sum() < 2:
        share = pd.to_numeric(df.get("subject_share"), errors="coerce")
        hits = pd.to_numeric(df.get("on_topic_hits"), errors="coerce")
        if isinstance(share, pd.Series) and isinstance(hits, pd.Series):
            relevance = share * np.sqrt(
                hits.fillna(0).clip(lower=0).astype(float)
            )
        else:
            relevance = pd.Series(np.nan, index=df.index)
    else:
        relevance = pd.to_numeric(relevance, errors="coerce")

    score = (
        0.30 * pct(relevance)
        + 0.25 * pct(odds)
        + 0.20 * pct(speed, invert=True)
        + 0.10 * pct(df.get("growth_rate"))
        + 0.10 * pct(df.get("submissions_now_est"), invert=True)
        + 0.05 * pct(df.get("review_days_iqr"), invert=True)
    )
    return (score * 100).round(1)


def recommend(
    df: pd.DataFrame,
    quartile: int | list[int] | None = None,
    top_n: int = 10,
    include_flagged: bool = False,
    min_articles: int = 20,
    max_per_publisher: int = 3,
    require_scopus: bool = True,
    sort_by: str = "fit",
) -> pd.DataFrame:
    """Top journals by acceptance odds, filtered to a quartile and quality floor."""
    out = df.copy()

    if quartile is not None:
        wanted = [int(q) for q in
                  ([quartile] if isinstance(quartile, int) else list(quartile))]

        # A quartile the tool inferred is not a quartile the client can quote.
        # When no SCImago entry exists, model.py fills the gap with a
        # within-batch citation percentile — useful for scoring, worthless as
        # an answer to "show me Q1 only", because it ranks journals against
        # whatever this search happened to return rather than against their
        # field. Asking for a quartile therefore means asking for a real one.
        if "quartile_is_proxy" in out.columns:
            out = out[~out["quartile_is_proxy"].fillna(False).astype(bool)]

        q = pd.to_numeric(out["quartile"], errors="coerce")
        out = out[q.isin(wanted)]

    # Too small to judge, and a thin journal is a risky home anyway.
    out = out[out["articles_latest"].fillna(0) >= min_articles]

    if out.empty:
        return out

    out["quality_flags"] = out.apply(lambda r: "; ".join(quality_flags(r)), axis=1)
    out["flag_count"] = out.apply(lambda r: len(quality_flags(r)), axis=1)
    out["opportunity_score"] = opportunity_score(out)

    # The Scopus gate only applies when Scopus is one of the indexes the
    # client asked for. Someone shortlisting on ABDC or PubMed alone has a
    # different requirement, and dropping every non-Scopus journal would
    # silently answer a question they did not ask. Coverage is still recorded
    # and still flagged, so a delisted title never passes unnoticed.
    if require_scopus and "scopus_status" in out.columns:
        # Delisted AND inactive are both disqualifying: in either case a paper
        # published there is not a Scopus publication.
        out = out[~out["scopus_status"].isin(["DISCONTINUED", "INACTIVE"])]

        # "We could not check" is not the same as "it is indexed".
        if not include_flagged:
            out = out[out["scopus_status"] == "active"]

    # A clearly-unrelated journal is excluded outright, not merely flagged.
    # This is the guard that would have stopped a sustainability journal
    # topping a web-services shortlist.
    if not include_flagged and "semantic_relevance" in out.columns:
        sem = pd.to_numeric(out["semantic_relevance"], errors="coerce")
        out = out[sem.isna() | (sem >= 0.20)]

    if not include_flagged:
        # Two or more warning signs keeps a journal off the shortlist.
        out = out[out["flag_count"] < 2]

    out = add_publisher_column(out)

    # One publisher's standards, APCs and turnaround are correlated across its
    # whole portfolio, so ten of its journals is one option shown ten times.
    if max_per_publisher:
        out = enforce_diversity(out, max_per_publisher=max_per_publisher)

    if sort_by == "speed" and "acceptance_days" in out.columns:
        # fastest known acceptance first; unknown speed last; fit breaks ties
        days = pd.to_numeric(out["acceptance_days"], errors="coerce")
        suspicious = out.get("acceptance_speed", pd.Series("", index=out.index)) \
            .eq("Suspiciously fast")
        out = out.assign(_d=days.where(~suspicious).fillna(10_000))
        return out.sort_values(["_d", "opportunity_score"],
                               ascending=[True, False]).drop(columns="_d").head(top_n)
    return out.sort_values("opportunity_score", ascending=False).head(top_n)


def format_shortlist(rec: pd.DataFrame, quartile_label: str = "any") -> str:
    if rec.empty:
        return (
            f"No journals passed the quality floor for quartile {quartile_label}.\n"
            "If the Scopus spreadsheets are missing from ./data/, every journal "
            "shows coverage 'unknown' and is held back — add them first.\n"
            "Either the batch was too small, or everything in it tripped two or "
            "more warning signs. Widen --limit, or use --include-flagged to see "
            "what was excluded and why."
        )

    lines = [f"Best acceptance odds — quartile {quartile_label}", ""]
    for i, (_, r) in enumerate(rec.iterrows(), 1):
        q = r.get("quartile")
        qs = f"Q{int(q)}" if pd.notna(q) else "Q?"
        if r.get("quartile_is_proxy"):
            qs += "*"

        rate = r.get("acceptance_rate_used")
        rate_s = f"{rate:.0%}" if pd.notna(rate) else "unknown"
        tier = r.get("evidence_tier", "-")
        subs = r.get("submissions_now_est")
        subs_s = f"~{int(subs):,}/yr" if pd.notna(subs) else "unknown"
        measured = r.get("review_days_median")
        if pd.notna(measured):
            iqr_lo, iqr_hi = r.get("review_days_p25"), r.get("review_days_p75")
            if pd.notna(iqr_lo) and pd.notna(iqr_hi):
                days_s = (f", review {int(measured)}d "
                          f"(usually {int(iqr_lo)}-{int(iqr_hi)}d)")
            else:
                days_s = f", review {int(measured)}d"
        else:
            days = r.get("days_to_first_decision")
            days_s = (f", {int(days)}d to first decision"
                      if pd.notna(days) else ", turnaround unknown")

        pub = str(r.get("publisher_group", ""))[:22]
        lines.append(
            f"{i:>2}. {str(r['display_name'])[:52]}  [{qs}]  "
            f"score {r['opportunity_score']:.0f}"
        )
        if pub and pub != "Unknown":
            lines.append(f"    {pub}")
        if "acceptance_days" in r.index:
            from .speed import display as speed_display
            ssrc = r.get("speed_source")
            lines.append(f"    Acceptance time: {speed_display(r.to_dict())}"
                         + (f"  [{ssrc}]" if isinstance(ssrc, str) and ssrc else ""))
        if "apc_status" in r.index and pd.notna(r.get("apc_status")):
            from .apc import display as apc_display
            src = r.get("apc_source")
            src_s = f"  [{src}]" if isinstance(src, str) and src else ""
            lines.append(f"    APC: {apc_display(r.to_dict())}{src_s}")
            fo = r.get("free_option")
            if isinstance(fo, str) and fo:
                note = r.get("free_option_note")
                lines.append(f"    Free option: {fo}"
                             + (f" — {note}" if isinstance(note, str) and note else ""))
        hits = r.get("on_topic_hits")
        shr = r.get("subject_share")
        sem = r.get("semantic_relevance")
        bits = []
        if pd.notna(sem):
            bits.append(f"subject match {sem:.2f}")
        if pd.notna(hits) and hits:
            shr_s = f" ({shr:.0%} of output)" if pd.notna(shr) else ""
            bits.append(f"{int(hits)} papers on topic{shr_s}")
        if bits:
            lines.append("    " + ", ".join(bits))
        lines.append(
            f"    accepts {rate_s} (tier {tier}), {subs_s} submissions"
            f"{days_s}, {int(r['articles_latest'])} articles/yr"
        )
        if r.get("quality_flags"):
            lines.append(f"    ! {r['quality_flags']}")
        lines.append("")

    lines.append(
        "Review times are MEASURED from Crossref received/accepted dates, not "
        "journal claims. 'turnaround unknown' means the publisher does not "
        "deposit those dates — roughly half do not.\n"
    )
    lines.append(
        "Score is relative to this batch, not absolute. A high score means "
        "better odds than the other journals here — not that the journal is "
        "good. Check tier: A/B rest on declared rates, C on the model, "
        "D on a prior. Asterisked quartiles are citation proxies, not Scopus."
    )
    return "\n".join(lines)
