"""Interactive mode: paste a paper title, get a journal shortlist.

Wraps the same pipeline as the `estimate` subcommand, but asks questions
instead of requiring flags. Intended as the default way to use the tool for
a single manuscript.

Flow:
    title -> extracted search terms -> you pick which -> per-term runs
    -> merged, deduplicated shortlist, with journals appearing in more than
       one term's results marked as the best scope fit
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from .recommend import format_shortlist, recommend
from .indexing import IndexRegistry, apply_index_filters
from .scimago import ScimagoIndex
from .scopus import ScopusIndex
from .sources import OpenAlexClient
from .terms import terms_from_title

OUT_DIR = Path("output")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)
    return val or (default or "")


def ask_yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({d})").lower()
    if not val:
        return default
    return val.startswith("y")


def pick_terms(terms, max_pick: int = 3) -> list[str]:
    print("\nSearch terms found for this title:\n")
    for i, t in enumerate(terms, 1):
        print(f"  {i}. {t}")

    print(
        "\nEach term is searched separately — journals that appear under more\n"
        "than one are usually the best scope fit for a cross-disciplinary paper."
    )
    raw = ask(
        f"\nWhich to use? numbers separated by commas, or Enter for the top "
        f"{min(max_pick, len(terms))}"
    )

    if not raw:
        return [t.text for t in terms[:max_pick]]

    chosen = []
    for part in raw.replace(" ", "").split(","):
        try:
            idx = int(part) - 1
            if 0 <= idx < len(terms):
                chosen.append(terms[idx].text)
        except ValueError:
            continue
    return chosen or [t.text for t in terms[:max_pick]]


def _reporter(args):
    """Progress callback for a UI, or a no-op on the command line.

    The CLI prints its own stage lines, so a caller that wants a progress bar
    passes `args.progress = fn` and gets (fraction, label) pairs instead of
    having to parse stdout.
    """
    fn = getattr(args, "progress", None)
    if not callable(fn):
        return lambda frac, label: None

    def report(frac: float, label: str) -> None:
        try:
            fn(max(0.0, min(1.0, float(frac))), label)
        except Exception:  # noqa: BLE001 — a broken UI must not kill a run
            pass

    return report


def clear_outputs() -> None:
    """Delete the previous run's CSVs before starting a new one.

    A run that shortlists nothing used to leave the last run's files in
    place, and any UI reading output/shortlist.csv would show them as if
    they were the new result — a Q3-only search silently displaying the Q1
    journals from the run before it. Clearing up front means an empty
    result looks empty.
    """
    for name in ("shortlist.csv", "all_journals.csv"):
        try:
            (OUT_DIR / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:                     # locked file, read-only mount
            print(f"  ! could not remove output/{name}: {exc}")


def run_interactive(args) -> int:
    report = _reporter(args)
    report(0.01, "Starting up")
    clear_outputs()
    print("=" * 70)
    print("Journal shortlist from a paper title")
    print("=" * 70)

    title = " ".join(args.title) if args.title else ask("\nPaper title")
    if not title:
        print("No title given.")
        return 1

    client = OpenAlexClient()

    scopus = ScopusIndex()
    scimago = ScimagoIndex()
    indexes = IndexRegistry()
    for line in indexes.summaries():
        print(f"  {line}")
    print(f"\n{scimago.summary()}")
    require_scopus = getattr(args, "require_scopus", True)
    if scopus.active:
        print(f"\n{scopus.summary()}")
        scopus_only = require_scopus
        if not require_scopus:
            print("  Scopus indexing not required — coverage is reported, "
                  "not enforced.")
    elif not require_scopus:
        print("\nNo Scopus list in ./data/ — not needed, Scopus is not "
              "one of the indexes you required.")
        scopus_only = False
    else:
        print(
            "\n! No Scopus list in ./data/ — coverage cannot be verified, so\n"
            "  non-indexed journals will appear and the shortlist will be empty.\n"
            "  Download ext_list_*.xlsx from elsevier.com/products/scopus/content"
        )
        if not ask_yes("  Continue anyway?", default=False):
            return 1
        scopus_only = False

    if getattr(args, "term", None):
        # Term supplied explicitly — skip extraction entirely.
        selected = list(args.term)
        print(f"\nUsing your term(s): {', '.join(selected)}")
    else:
        print("\nWorking out what this paper is about...")
        try:
            terms = terms_from_title(client, title)
        except ConnectionError as exc:
            print(f"\n{exc}")
            return 1

        if not terms:
            print(
                "\nCould not extract usable terms from this title.\n"
                "Supply one yourself with --term, e.g.:\n"
                '  python -m journal_traffic.cli find "your title" '
                '--term "web service quality of service"'
            )
            return 1

        selected = pick_terms(terms)

    # Quartiles may arrive from a flag or a GUI control; only fall back to
    # prompting when nothing was supplied.
    quartiles: list[int] = []
    supplied = getattr(args, "quartile", None)
    if supplied:
        quartiles = [int(q) for q in supplied if 1 <= int(q) <= 4]
        print(f"\nRestricting to: {', '.join(f'Q{q}' for q in quartiles)}")
    else:
        q_raw = ask("\nRestrict to quartiles? e.g. 1 2, or Enter for all")
        for part in q_raw.replace(",", " ").split():
            if part.isdigit() and 1 <= int(part) <= 4:
                quartiles.append(int(part))

    per_term = args.limit
    print(f"\nSearching {len(selected)} term(s), up to {per_term} journals each.")
    print("Roughly 2-4 minutes on a first run; everything caches for a week,")
    print("so re-runs on the same terms are near-instant.")

    import time

    from .acceptance import fetch_report, harvest_many
    from .publishers import add_publisher_column, diversity_report
    from .semantic import (SemanticMatcher, fetch_journal_corpora,
                           query_topics_from_openalex)
    from .turnaround import measure_many
    from .lag import apply_lag_correction
    from .model import AcceptanceModel, build_features, estimate_traffic
    from .scopus import parse_quartile
    from .sources import iter_records
    from dataclasses import asdict

    years = list(range(args.year - 3, args.year + 1))
    all_rows: dict[str, dict] = {}
    term_hits: dict[str, set] = {}

    t0 = time.time()

    # Searching is the long pole, so it owns most of the bar: 5% to 55%.
    for n, term in enumerate(selected, 1):
        report(0.05 + 0.50 * (n - 1) / len(selected),
               f"Searching term {n} of {len(selected)}: {term}")
        print(f"\n[{n}/{len(selected)}] searching: {term}")
        try:
            sources = client.journals_by_subject(term, limit=per_term)
        except Exception as exc:  # noqa: BLE001
            print(f"    search failed: {type(exc).__name__}: {exc}")
            continue

        print(f"    {len(sources)} candidate journals; fetching metrics")
        kept = skipped = 0

        span_lo = 0.05 + 0.50 * (n - 1) / len(selected)
        span_hi = 0.05 + 0.50 * n / len(selected)

        for seen, rec in enumerate(
            iter_records(client, sources, years, composition=not args.fast), 1
        ):
            # Fine-grained movement inside a term, so the bar never sits
            # still for the several minutes one term can take.
            if seen % 5 == 0 and sources:
                report(
                    span_lo + (span_hi - span_lo) * min(1.0, seen / len(sources)),
                    f"Term {n}/{len(selected)}: {seen}/{len(sources)} journals "
                    f"· {len(all_rows)} kept",
                )
            status = scopus.status(
                rec.issns + ([rec.issn_l] if rec.issn_l else [])
            )
            if scopus_only and status["scopus_status"] != "active":
                skipped += 1
                continue

            key = rec.issn_l or rec.openalex_id
            term_hits.setdefault(key, set()).add(term)
            if key in all_rows:
                continue

            row = asdict(rec)
            row.update(status)
            # Real SJR quartile where SCImago has the journal; the
            # citation-percentile proxy only fills the gaps.
            row["quartile"] = parse_quartile(status.get("quartile"))
            sj = scimago.lookup(
                rec.issns + ([rec.issn_l] if rec.issn_l else []), subject=term
            )
            if sj.get("quartile"):
                row["quartile"] = sj["quartile"]
                row["quartile_source"] = sj["quartile_source"]
                row["sjr_value"] = sj.get("sjr_value")
                row["sjr_categories"] = "; ".join(
                    f"{k} (Q{v})" for k, v in (sj.get("sjr_categories") or {}).items()
                )
            row.update(indexes.annotate(
                rec.issns + ([rec.issn_l] if rec.issn_l else []),
                rec.display_name,
            ))
            all_rows[key] = row
            kept += 1

        print(f"    kept {kept}, skipped {skipped} (not Scopus-active); "
              f"{len(all_rows)} unique so far  [{time.time() - t0:.0f}s]")

    # APCs: DOAJ, then OpenAlex, then the publisher's own page. Cheap next to
    # the acceptance scrape, and the first thing a client asks about.
    if not getattr(args, "no_apc", False) and all_rows:
        from .apc import lookup_many, summary as apc_summary
        report(0.56, f"Looking up APC charges ({len(all_rows)} journals)")
        print(f"\nLooking up article processing charges ({len(all_rows)} journals)")
        apcs = lookup_many(all_rows, scrape=not args.no_scrape)
        for key, info in apcs.items():
            row = all_rows[key]
            row.update(info.as_row())
            # feed the better figure to the model and the high-APC flag
            if info.apc_usd_est is not None and not row.get("apc_usd"):
                row["apc_usd"] = info.apc_usd_est
        print("  " + apc_summary(apcs))

    max_apc = getattr(args, "max_apc_usd", None)

    # Scraping is batched and parallel: sequentially it dominated the runtime,
    # since almost all of it is waiting on publisher servers.
    report(0.55, f"{len(all_rows)} journals found")

    if not args.no_scrape and all_rows:
        report(0.58, f"Reading publisher pages for acceptance rates ({len(all_rows)})")
        print(f"\nLooking for declared acceptance rates ({len(all_rows)} journals)")
        pages = [(k, r.get("homepage")) for k, r in all_rows.items()]
        evidence = harvest_many(pages)
        for key, ev in evidence.items():
            all_rows[key]["declared_acceptance_rate"] = ev.acceptance_rate
            all_rows[key]["declared_submissions"] = ev.submissions
            all_rows[key]["days_to_first_decision"] = ev.days_to_first_decision
            for sk, sv in (ev.stated_speed or {}).items():
                all_rows[key][sk] = sv

        found = sum(1 for e in evidence.values() if e.acceptance_rate is not None)
        print(f"\n  {found} declared acceptance rate(s) found "
              f"({15 - found} more needed to train the model)"
              if found < 15 else f"\n  {found} declared acceptance rate(s) found")
        print("  " + fetch_report().replace("\n", "\n  "))
    else:
        for r in all_rows.values():
            r.setdefault("declared_acceptance_rate", None)
            r.setdefault("declared_submissions", None)
            r.setdefault("days_to_first_decision", None)

    # Measured review times from Crossref deposit dates. This is the only
    # turnaround signal with real data behind it — declared decision times
    # are almost never published.
    if not args.no_turnaround and all_rows:
        report(0.70, f"Measuring review turnaround ({len(all_rows)} journals)")
        print(f"\nMeasuring review turnaround ({len(all_rows)} journals)")
        pairs = [(k, r.get("issn_l") or (r.get("issns") or [None])[0])
                 for k, r in all_rows.items()]
        ta = measure_many([(k, i) for k, i in pairs if i])
        for key, t in ta.items():
            if key not in all_rows:
                continue
            all_rows[key]["review_days_median"] = (
                t.review_median if t.is_reliable else None
            )
            all_rows[key]["review_days_p25"] = t.review_p25 if t.is_reliable else None
            all_rows[key]["review_days_p75"] = t.review_p75 if t.is_reliable else None
            all_rows[key]["review_days_iqr"] = t.spread if t.is_reliable else None
            all_rows[key]["production_days"] = t.production_median
        n_ok = sum(1 for t in ta.values() if t.is_reliable)
        print(f"  {n_ok}/{len(ta)} journals had usable received/accepted dates")
        if n_ok < len(ta) * 0.3:
            print("  (many publishers do not deposit these dates; those "
                  "journals score neutral on speed rather than being penalised)")

    # Semantic relevance. Keyword hit-counts cannot tell a paper about web
    # service QoS from one about service quality in public transport; comparing
    # the journal's actual published titles can.
    if not args.no_semantic and all_rows:
        report(0.82, f"Profiling what each journal publishes ({len(all_rows)})")
        print(f"\nProfiling what each journal actually publishes "
              f"({len(all_rows)} journals)")
        query_text = " ".join([title] + selected)

        corpora, jtopics = fetch_journal_corpora(
            client, {k: r["openalex_id"] for k, r in all_rows.items()}
        )
        matcher = SemanticMatcher(query_text)
        matcher.set_query_topics(query_topics_from_openalex(client, query_text))

        tfidf = matcher.tfidf_scores(corpora)
        tscores = matcher.topic_scores(jtopics)
        keyword = {
            k: (r.get("subject_share") or 0.0) for k, r in all_rows.items()
        }
        relevance = matcher.combine(tfidf, tscores, keyword)

        for key, score in relevance.items():
            if key in all_rows:
                all_rows[key]["semantic_relevance"] = score
                all_rows[key]["tfidf_similarity"] = tfidf.get(key)
                all_rows[key]["topic_overlap"] = tscores.get(key)

        strong = sum(1 for v in relevance.values() if v >= 0.5)
        print(f"  {strong}/{len(relevance)} journals closely match the paper")

    # Acceptance speed: measured Crossref dates first, the journal's own
    # stated figure second, clearly labelled either way.
    from .speed import classify as speed_classify
    for r in all_rows.values():
        r.update(speed_classify(r))

    report(0.92, "Scoring and ranking")
    print(f"\nData collection done in {time.time() - t0:.0f}s. Scoring...")

    if not all_rows:
        print(
            "\nNo Scopus-indexed journals found for these terms. Try broader\n"
            "terms, or check that ext_list_*.xlsx is in ./data/."
        )
        return 1

    df = build_features(list(all_rows.values()))
    df["term_matches"] = [len(term_hits.get(k, ())) for k in all_rows]

    model = None
    try:
        candidate = AcceptanceModel()
        print("\n" + candidate.fit(df).summary())
        model = candidate          # only adopt it once fit() has succeeded
    except ValueError as exc:
        print(f"\nModel not trained: {exc}")
        print("Falling back to quartile priors — every journal will be tier D.")

    result = apply_lag_correction(estimate_traffic(df, model), args.year)

    result = add_publisher_column(result)
    print("\n" + diversity_report(result))

    # List-membership filters run before scoring the shortlist, so the top-N
    # is chosen from journals that already satisfy the client's requirement
    # rather than being cut down to nothing afterwards.
    if max_apc is not None and "apc_usd_est" in result.columns:
        # budget filter: journals whose charge is known to exceed it go;
        # no-APC and subscription journals stay; unknown stays but is flagged
        est = pd.to_numeric(result["apc_usd_est"], errors="coerce")
        free = result.get("free_option", pd.Series("", index=result.index)) \
            .fillna("").eq("Yes")
        # a hybrid journal over budget stays: the author can publish free
        over = (est > float(max_apc)) & ~free
        n_over = int(over.sum())
        result = result[~over]
        print(f"\nAPC budget ${int(max_apc):,}: {n_over} journal(s) over budget removed. "
              "Journals whose charge is unknown are kept — check them.")
        if result.empty:
            print("\nEvery journal with a known charge is over the APC budget.")
            return 1

    fast_days = getattr(args, "fast_days", None)
    if fast_days and "acceptance_days" in result.columns:
        from .speed import is_fast
        n0 = len(result)
        unknown = int(result["acceptance_days"].isna().sum())
        result = result[[is_fast(r, int(fast_days)) for r in result.to_dict("records")]]
        print(f"\nFast-acceptance filter (<= {int(fast_days)} days to acceptance): "
              f"{len(result)}/{n0} journals qualify; {unknown} had no measured or "
              "stated review time and were left out.")
        if result.empty:
            print("\nNo journal in this batch has a known acceptance time that short. "
                  "Raise the day limit, or keep 'Measured review turnaround' on.")
            return 1

    if getattr(args, "free_only", False) and "free_option" in result.columns:
        n0 = len(result)
        result = result[result["free_option"].isin(["Yes", "Waiver on request"])]
        print(f"\nFree-option filter: {len(result)}/{n0} journals can be published "
              "in without paying (no APC, subscription route, or waiver).")
        if result.empty:
            print("\nNo journal in this batch offers a free publishing route.")
            return 1

    before = len(result)
    result = apply_index_filters(
        result,
        require_pubmed=getattr(args, "require_pubmed", False),
        abdc_ranks=getattr(args, "abdc_ranks", None),
    )
    if len(result) != before:
        print(f"\nIndex filters: {len(result)}/{before} journals qualify.")
    if result.empty:
        print("\nNo journal in this batch is on every list you required.")
        return 1

    if quartiles and "quartile_is_proxy" in result.columns:
        n_proxy = int(result["quartile_is_proxy"].fillna(False).sum())
        if n_proxy:
            print(f"\n{n_proxy} journal(s) excluded: no real SJR quartile, only "
                  f"a within-batch proxy. Refresh the SCImago file to cover more.")

    rec_df = recommend(
        result,
        quartile=quartiles or None,
        top_n=args.top,
        max_per_publisher=args.max_per_publisher,
        require_scopus=require_scopus,
        sort_by=getattr(args, "sort_by", "fit"),
    )

    # A journal matching several terms fits a cross-disciplinary paper better
    # than one matching only the strongest term.
    if (not rec_df.empty and "term_matches" in rec_df.columns
            and getattr(args, "sort_by", "fit") != "speed"):
        rec_df = rec_df.sort_values(
            ["term_matches", "opportunity_score"], ascending=False
        )

    label = ", ".join(f"Q{q}" for q in quartiles) if quartiles else "any"
    print("\n" + "=" * 70)
    print(format_shortlist(rec_df, label))

    OUT_DIR.mkdir(exist_ok=True)
    if "apc_status" in result.columns:
        from .apc import display as apc_display
        result["apc"] = [apc_display(r) for r in result.to_dict("records")]
        if not rec_df.empty:
            rec_df["apc"] = [apc_display(r) for r in rec_df.to_dict("records")]
    result.to_csv(OUT_DIR / "all_journals.csv", index=False)

    if rec_df.empty:
        print(
            f"\nNothing matched. {len(result)} journal(s) were found and "
            f"scored, but none passed the filters you set"
            + (f" (quartile {label})" if quartiles else "")
            + ". output/all_journals.csv has the full set, unfiltered."
        )
        report(1.0, "Done — no journal matched your filters")
        return 0

    if not rec_df.empty:
        multi = rec_df[rec_df["term_matches"] > 1]
        if not multi.empty:
            print("\nBest scope fit (matched more than one search term):")
            for _, r in multi.iterrows():
                terms_str = ", ".join(sorted(term_hits[r.get("issn_l") or ""]))
                print(f"  · {r['display_name']}  [{terms_str}]")

        path = OUT_DIR / "shortlist.csv"
        rec_df.to_csv(path, index=False)
        print(f"\nWritten to {path} and output/all_journals.csv")

    report(1.0, f"Done — {len(rec_df)} journals shortlisted")
    return 0
