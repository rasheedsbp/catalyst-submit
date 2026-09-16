"""CLI: estimate submission traffic for Scopus-indexed journals in a subject area.

Two subcommands:

  find      — paste a paper title, get a shortlist (guided; start here)
  estimate  — one-off analysis of a subject area or ISSN shortlist
  track     — weekly snapshot of acceptance throughput; builds a time series

Examples
--------
    python -m journal_traffic.cli find
    python -m journal_traffic.cli find "A Digital Twin Framework for Net Zero Buildings"
    python -m journal_traffic.cli estimate --subject "wireless sensor networks" --limit 60
    python -m journal_traffic.cli estimate --subject "fog computing" --recommend
    python -m journal_traffic.cli track --issn-file shortlist.txt
    python -m journal_traffic.cli track --report
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .acceptance import fetch_report, harvest
from .lag import apply_lag_correction, reconcile_with_realtime
from .model import AcceptanceModel, build_features, estimate_traffic
from .realtime import CrossrefTracker, connect, trend
from .interactive import run_interactive
from .publishers import add_publisher_column, diversity_report
from .recommend import format_shortlist, recommend
from .scimago import ScimagoIndex
from .scopus import ScopusIndex, parse_quartile
from .turnaround import measure_many
from .sources import OpenAlexClient, iter_records

OUT_DIR = Path("output")


def read_issns(path: str) -> list[str]:
    return [
        line.strip()
        for line in Path(path).read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def collect(args) -> list[dict]:
    client = OpenAlexClient()
    years = list(range(args.year - args.window + 1, args.year + 1))

    if args.issn_file:
        issns = read_issns(args.issn_file)
        print(f"Resolving {len(issns)} ISSNs...")
        sources = [s for s in (client.source_by_issn(i) for i in issns) if s]
    else:
        print(f"Finding journals in '{args.subject}'...")
        sources = client.journals_by_subject(
            args.subject, limit=args.limit, debug=args.debug
        )

    print(f"Found {len(sources)} journals. Pulling metrics and article counts...")

    scopus = ScopusIndex()
    scimago = ScimagoIndex()
    print(f"  {scimago.summary()}")
    if not scopus.active:
        print(
            "\n  !! No Scopus source list found in ./data/\n"
            "     OpenAlex indexes roughly twice as many journals as Scopus, so\n"
            "     this run WILL include non-Scopus titles and cannot tell you\n"
            "     which. Download the Scopus source list and discontinued-titles\n"
            "     spreadsheets from Elsevier into ./data/ to filter them out.\n"
        )
        if getattr(args, "scopus_only", False):
            print("     --scopus-only requires those files. Aborting.\n")
            return []
    else:
        print(f"  {scopus.summary()}")

    rows = []
    for rec in iter_records(client, sources, years,
                            composition=not getattr(args, "fast", False)):
        row = asdict(rec)
        status = scopus.status(rec.issns + ([rec.issn_l] if rec.issn_l else []))
        row.update(status)
        row["quartile"] = parse_quartile(status.get("quartile"))
        subject = getattr(args, "subject", None)
        sj = scimago.lookup(
            rec.issns + ([rec.issn_l] if rec.issn_l else []), subject=subject
        )
        if sj.get("quartile"):
            row["quartile"] = sj["quartile"]
            row["quartile_source"] = sj["quartile_source"]
            row["sjr_value"] = sj.get("sjr_value")

        if not args.no_scrape:
            ev = harvest(rec.homepage)
            row["declared_acceptance_rate"] = ev.acceptance_rate
            row["declared_submissions"] = ev.submissions
            row["days_to_first_decision"] = ev.days_to_first_decision
            row["acceptance_source"] = ev.source_url if ev.tier else None

        if getattr(args, "scopus_only", False) and status["scopus_status"] != "active":
            # INACTIVE and DISCONTINUED both mean a paper published there now
            # will not be a Scopus publication.
            print(f"  - {rec.display_name[:55]:<55} skipped ({status['scopus_status']})")
            continue

        rows.append(row)
        print(f"  · {rec.display_name[:55]:<55} {status['scopus_status']}")

    return rows


# ---------------------------------------------------------------- estimate

def cmd_estimate(args) -> int:
    OUT_DIR.mkdir(exist_ok=True)
    rows = collect(args)
    if not rows:
        print("No journals resolved.", file=sys.stderr)
        return 1

    df = build_features(rows)

    if args.labels:
        labels = pd.read_csv(args.labels)
        lookup = {
            str(r["issn"]).replace("-", ""): float(r["rate"])
            for _, r in labels.iterrows()
        }
        df["declared_acceptance_rate"] = df.apply(
            lambda r: next(
                (lookup[i.replace("-", "")] for i in (r["issns"] or [])
                 if i.replace("-", "") in lookup),
                r.get("declared_acceptance_rate"),
            ),
            axis=1,
        )

    model = None
    try:
        model = AcceptanceModel()
        print("\n" + model.fit(df).summary() + "\n")
        model.save(OUT_DIR / "acceptance_model.joblib")
    except ValueError as exc:
        print(f"\nModel not trained: {exc}")
        print("Falling back to quartile priors (tier D).\n")
        model = None

    result = estimate_traffic(df, model)

    # Live throughput, if asked for. Also supplies observed pipeline lag,
    # which materially improves the projection below.
    snapshots = {}
    if args.realtime:
        print("Pulling live acceptance throughput from Crossref...")
        tracker = CrossrefTracker()
        conn = connect()
        for _, r in result.iterrows():
            issn = r.get("issn_l") or (r.get("issns") or [None])[0]
            if not issn:
                continue
            snap = tracker.snapshot(str(issn), r.get("display_name"))
            if snap:
                snapshots[str(issn)] = snap
                tracker.record(conn, snap)
        conn.close()
        result["median_lag_days"] = result.apply(
            lambda r: getattr(
                snapshots.get(str(r.get("issn_l") or "")), "median_lag_days", None
            ),
            axis=1,
        )
        print(f"  got {len(snapshots)} snapshots\n")

    if not args.no_turnaround:
        print(f"\nMeasuring review turnaround ({len(result)} journals)")
        pairs = [
            (i, r.get("issn_l") or (r.get("issns") or [None])[0])
            for i, r in result.iterrows()
        ]
        ta = measure_many([(k, v) for k, v in pairs if v])
        result["review_days_median"] = [
            (ta[i].review_median if i in ta and ta[i].is_reliable else None)
            for i in result.index
        ]
        result["review_days_p25"] = [
            (ta[i].review_p25 if i in ta and ta[i].is_reliable else None)
            for i in result.index
        ]
        result["review_days_p75"] = [
            (ta[i].review_p75 if i in ta and ta[i].is_reliable else None)
            for i in result.index
        ]
        result["review_days_iqr"] = [
            (ta[i].spread if i in ta and ta[i].is_reliable else None)
            for i in result.index
        ]
        n_ok = sum(1 for t in ta.values() if t.is_reliable)
        print(f"  {n_ok}/{len(ta)} journals had usable received/accepted dates")

    result = apply_lag_correction(result, reference_year=args.year)
    if snapshots:
        result = reconcile_with_realtime(result, snapshots)

    if args.quartile:
        result = result[result["quartile"].isin(args.quartile)]

    cols = [
        "display_name", "publisher", "issn_l", "scopus_status", "quartile",
        "quartile_is_proxy", "quartile_source", "sjr_value",
        "citescore", "articles_latest", "growth_rate", "special_issue_share",
        "acceptance_rate_used", "evidence_tier",
        "submissions_est", "pipeline_lag_months", "lag_source",
        "submissions_now_low", "submissions_now_est", "submissions_now_high",
        "projection_confidence", "regular_track_odds",
        "review_days_median", "review_days_p25", "review_days_p75",
        "review_days_iqr", "production_days",
        "observed_annual_output", "output_divergence", "realtime_flag",
        "days_to_first_decision", "acceptance_source",
    ]
    cols = [c for c in cols if c in result.columns]
    table = result[cols].sort_values("submissions_now_est", ascending=False)

    path = OUT_DIR / args.out
    table.to_csv(path, index=False)

    pd.set_option("display.width", 250, "display.max_columns", 50)
    show = [c for c in [
        "display_name", "quartile", "articles_latest", "acceptance_rate_used",
        "evidence_tier", "submissions_now_est", "projection_confidence",
        "regular_track_odds",
    ] if c in table.columns]
    print(table[show].head(25).to_string(index=False))

    if "scopus_status" in table:
        dead = table[table["scopus_status"] == "DISCONTINUED"]
        if not dead.empty:
            print(f"\n!! {len(dead)} discontinued from Scopus — "
                  "do not route clients here:")
            for _, r in dead.iterrows():
                print(f"   - {r['display_name']}")

    if "realtime_flag" in table:
        flagged = table[table["realtime_flag"] != ""]
        for _, r in flagged.iterrows():
            print(f"\n?  {r['display_name']}: live output differs "
                  f"{r['output_divergence']:+.0%} from the published-year baseline. "
                  f"Growth rate or lag is probably off.")

    if result.get("quartile_is_proxy", pd.Series(dtype=bool)).any():
        n = int(result["quartile_is_proxy"].sum())
        print(f"\nNOTE: {n} quartile(s) are citation-percentile proxies, not "
              "Scopus quartiles. Add the Scopus spreadsheets to ./data/ for "
              "real quartiles and delisting detection.")

    if "scopus_status" in table.columns:
        counts = table["scopus_status"].value_counts().to_dict()
        print(f"\nScopus coverage: {counts}")
        if counts.get("unknown"):
            print(f"  {counts['unknown']} journal(s) not matched to the Scopus "
                  "list — either not indexed, or an ISSN mismatch. Use "
                  "--scopus-only to exclude them.")

    print(f"\nEvidence tiers: {table['evidence_tier'].value_counts().to_dict()}"
          "  (A/B = declared, C = model, D = prior)")
    if "projection_confidence" in table:
        print(f"Projection confidence: "
              f"{table['projection_confidence'].value_counts().to_dict()}")
    if args.recommend:
        qs = args.recommend_quartile
        label = ", ".join(f"Q{q}" for q in qs) if qs else "any"
        result = add_publisher_column(result)
        print("\n" + diversity_report(result))
        rec = recommend(
            result,
            quartile=qs if qs else None,
            top_n=args.top,
            include_flagged=args.include_flagged,
            max_per_publisher=args.max_per_publisher,
        )
        print("\n" + "=" * 78)
        print(format_shortlist(rec, label))
        if not rec.empty:
            rp = OUT_DIR / "shortlist.csv"
            rec.to_csv(rp, index=False)
            print(f"\nShortlist written to {rp}")

    print(f"Written to {path}")
    return 0


# ------------------------------------------------------------------- track

def cmd_track(args) -> int:
    conn = connect()

    if args.report:
        rows = conn.execute(
            "SELECT DISTINCT issn, journal FROM inpress_snapshots"
        ).fetchall()
        if not rows:
            print("No snapshots yet. Run `track --issn-file ...` first.")
            conn.close()
            return 0
        print(f"{'Journal':<48} {'Snaps':>5} {'91d out':>8} {'D/wk':>7}  Direction")
        print("-" * 88)
        for issn, name in rows:
            t = trend(conn, issn)
            if t["status"] != "ok":
                print(f"{(name or issn)[:48]:<48} {t['snapshots']:>5} "
                      f"{'-':>8} {'-':>7}  need {t['need']} snapshots")
                continue
            print(f"{(name or issn)[:48]:<48} {t['snapshots']:>5} "
                  f"{t['latest_91d_output']:>8} {t['weekly_change_pct']:>6.1f}%"
                  f"  {t['direction']}")
        conn.close()
        return 0

    if not args.issn_file:
        print("track needs --issn-file or --report", file=sys.stderr)
        conn.close()
        return 1

    issns = read_issns(args.issn_file)
    tracker = CrossrefTracker()
    print(f"Snapshotting {len(issns)} journals...")

    ok = 0
    for issn in issns:
        snap = tracker.snapshot(issn)
        if snap is None:
            print(f"  ! no Crossref data for {issn}")
            continue
        tracker.record(conn, snap)
        ok += 1
        print(f"  · {issn}  91d={snap.deposited_91d:>4}  "
              f"in-press={snap.inpress_count:>4}  "
              f"~{snap.accepted_per_week:.1f}/week")

    conn.close()
    print(f"\nRecorded {ok} snapshots. Run weekly; trends need 3+ points.")
    print("Then: python -m journal_traffic.cli track --report")
    return 0


# -------------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("estimate", help="Analyse a subject area or ISSN list")
    src = e.add_mutually_exclusive_group(required=True)
    src.add_argument("--subject", help="e.g. 'wireless sensor networks'")
    src.add_argument("--issn-file", help="One ISSN per line")
    e.add_argument("--limit", type=int, default=50)
    e.add_argument("--year", type=int, default=2025, help="Last complete year")
    e.add_argument("--window", type=int, default=4)
    e.add_argument("--quartile", type=int, nargs="*")
    e.add_argument("--labels", help="CSV: issn,rate")
    e.add_argument("--no-scrape", action="store_true")
    e.add_argument("--fast", action="store_true",
                   help="Skip special-issue sampling: ~2x faster")
    e.add_argument("--no-turnaround", action="store_true",
                   help="Skip Crossref review-time measurement (faster)")
    e.add_argument("--realtime", action="store_true",
                   help="Pull live Crossref throughput; sharpens the projection")
    e.add_argument("--scopus-only", action="store_true",
                   help="Drop journals not in the Scopus active source list "
                        "(requires the spreadsheets in ./data/)")
    e.add_argument("--recommend", action="store_true",
                   help="Also print a shortlist ranked by acceptance odds")
    e.add_argument("--recommend-quartile", type=int, nargs="*", default=None,
                   metavar="Q", help="Restrict the shortlist to these quartiles")
    e.add_argument("--max-per-publisher", type=int, default=3,
                   metavar="N", help="Cap journals per publisher in the "
                        "shortlist (0 = no cap)")
    e.add_argument("--top", type=int, default=10, help="Shortlist length")
    e.add_argument("--include-flagged", action="store_true",
                   help="Show journals that tripped quality warnings")
    e.add_argument("--debug", action="store_true",
                   help="Show which discovery strategy matched")
    e.add_argument("--out", default="journal_traffic.csv")
    e.set_defaults(func=cmd_estimate)

    f = sub.add_parser(
        "find", help="Paste a paper title, get a journal shortlist (guided)"
    )
    f.add_argument("title", nargs="*", help="Paper title (or you'll be asked)")
    f.add_argument("--year", type=int, default=2025)
    f.add_argument("--quartile", type=int, nargs="*", metavar="Q",
                   help="Restrict the shortlist to these quartiles, e.g. 1 2")
    f.add_argument("--term", action="append", metavar="PHRASE",
                   help="Search this phrase instead of extracting from the "
                        "title. Repeatable for several terms.")
    f.add_argument("--max-per-publisher", type=int, default=3,
                   metavar="N", help="Cap journals per publisher in the "
                        "shortlist (0 = no cap)")
    f.add_argument("--top", type=int, default=10)
    f.add_argument("--limit", type=int, default=40,
                   help="Journals per search term (lower = faster)")
    f.add_argument("--fast", action="store_true",
                   help="Skip special-issue/author-mix sampling: ~2x faster, "
                        "loses the regular-track odds correction")
    f.add_argument("--no-scrape", action="store_true",
                   help="Skip publisher pages entirely: much faster, but no "
                        "declared acceptance rates so everything is tier D")
    f.add_argument("--no-apc", action="store_true",
                   help="Skip the APC lookup (DOAJ / OpenAlex / publisher page)")
    f.add_argument("--max-apc", type=int, dest="max_apc_usd", metavar="USD",
                   help="Drop journals whose known APC exceeds this many USD")
    f.add_argument("--free-only", action="store_true",
                   help="Only journals you can publish in without paying "
                        "(no APC, subscription route, or waiver)")
    f.add_argument("--fast-accept", dest="fast_days", type=int, nargs="?", const=90,
                   metavar="DAYS",
                   help="Only journals whose measured/stated submission-to-"
                        "acceptance time is within DAYS (default 90)")
    f.add_argument("--sort-speed", dest="sort_by", action="store_const",
                   const="speed", default="fit",
                   help="Rank the shortlist by fastest acceptance instead of fit")
    f.add_argument("--no-turnaround", action="store_true",
                   help="Skip Crossref review-time measurement (faster)")
    f.add_argument("--no-scopus", action="store_false", dest="require_scopus",
                   help="Do not require Scopus indexing (default: required)")
    f.add_argument("--pubmed", action="store_true", dest="require_pubmed",
                   help="Keep only journals listed in PubMed/MEDLINE")
    f.add_argument("--abdc", nargs="*", dest="abdc_ranks", metavar="RANK",
                   help="Keep only these ABDC ratings, e.g. --abdc A* A")
    f.add_argument("--fetch-data", action="store_true",
                   help="Download/refresh the journal lists into data/ first")
    f.add_argument("--no-semantic", action="store_true",
                   help="Skip semantic relevance profiling (faster, but "
                        "relevance falls back to keyword hit counts)")
    f.set_defaults(func=run_interactive)

    t = sub.add_parser("track", help="Weekly throughput snapshot")
    t.add_argument("--issn-file")
    t.add_argument("--report", action="store_true", help="Show accumulated trends")
    t.set_defaults(func=cmd_track)

    args = p.parse_args(argv)

    if getattr(args, "fetch_data", False):
        from . import datasets

        print("Fetching journal lists into ./data/ ...")
        for r in datasets.fetch_all():
            print(f"  [{'ok ' if r.ok else 'no '}] {r.key}: {r.message}")
        print()

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
