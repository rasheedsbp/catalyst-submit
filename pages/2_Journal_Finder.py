"""Journal Finder — integrated page.

The pipeline is the original Journal Finder, unchanged. What differs is the
chrome: shared navigation, the CS Software Solutions theme, and the client
profile, so a returning researcher's title and usual journals come prefilled.
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Journal Finder · CS Software Solutions",
                   page_icon="🔍", layout="wide")

from core import theme, nav, client, services as svc_mod   # noqa: E402

svc_mod.init()
theme.header("Journal Finder", "Where should this paper go?")
nav.bar("pages/2_Journal_Finder.py")
theme.sidebar()
client.panel()

_g = client.greeting()
if _g:
    st.caption(_g)


import io
import sys
import time
import traceback
from pathlib import Path

import pandas as pd



DATA = Path("data")
OUT = Path("output")


# --------------------------------------------------------------- plumbing

class Args:
    """Stand-in for the argparse namespace the pipeline expects."""

    def __init__(self, **kw):
        self.title = kw.get("title", [])
        self.term = kw.get("term") or None
        self.limit = kw.get("limit", 40)
        self.top = kw.get("top", 10)
        self.year = kw.get("year", 2025)
        self.fast = kw.get("fast", True)
        self.no_scrape = kw.get("no_scrape", False)
        self.no_turnaround = kw.get("no_turnaround", False)
        self.max_per_publisher = kw.get("max_per_publisher", 3)
        self.quartile = kw.get("quartile") or None
        self.no_semantic = kw.get("no_semantic", False)
        self.require_scopus = kw.get("require_scopus", True)
        self.progress = kw.get("progress")
        self.require_pubmed = kw.get("require_pubmed", False)
        self.abdc_ranks = kw.get("abdc_ranks") or None
        self.no_apc = kw.get("no_apc", False)
        self.max_apc_usd = kw.get("max_apc_usd")
        self.free_only = kw.get("free_only", False)
        self.fast_days = kw.get("fast_days")
        self.sort_by = kw.get("sort_by", "fit")


@st.cache_resource(show_spinner=False)
def registry(stamp: float):
    """Index registry, rebuilt whenever the data folder changes."""
    from journal_traffic.indexing import IndexRegistry

    return IndexRegistry(DATA)


def data_stamp() -> float:
    if not DATA.exists():
        return 0.0
    return max((f.stat().st_mtime for f in DATA.iterdir() if f.is_file()),
               default=0.0)


@st.cache_data(show_spinner=False, ttl=3600)
def detect_terms(title: str) -> list[dict]:
    """Terms are derived, never typed — OpenAlex decides what the paper is about."""
    from journal_traffic.sources import OpenAlexClient
    from journal_traffic.terms import terms_from_title

    client = OpenAlexClient()
    return [
        {"text": t.text, "works": t.works, "source": t.source,
         "note": getattr(t, "note", "")}
        for t in terms_from_title(client, title)
    ]


def scopus_file() -> Path | None:
    if not DATA.exists():
        return None
    files = [f for f in list(DATA.glob("*.xlsx")) + list(DATA.glob("*.xls"))
             if "book" not in f.name.lower() and "abdc" not in f.name.lower()]
    return files[0] if files else None


def run_pipeline(args: Args) -> tuple[str, Exception | None]:
    """Run the search on a worker thread so the bar can move while it works.

    Streamlit reruns the script on every interaction and has no way to push
    an update from inside a long call, so the pipeline goes to a thread and
    the main thread polls its progress. Only the main thread touches `st`.
    """
    import builtins
    import threading

    from journal_traffic.interactive import run_interactive

    buf: io.StringIO = io.StringIO()
    state: dict = {"frac": 0.0, "label": "Starting up", "err": None}
    lock = threading.Lock()

    def report(frac: float, label: str) -> None:
        with lock:
            state["frac"], state["label"] = frac, label

    args.progress = report

    def work() -> None:
        real_stdout, real_input = sys.stdout, builtins.input
        try:
            sys.stdout = buf
            builtins.input = lambda prompt="": ""   # never block on a prompt
            run_interactive(args)
        except Exception as exc:                    # noqa: BLE001
            state["err"] = exc
            buf.write("\n" + traceback.format_exc())
        finally:
            sys.stdout = real_stdout
            builtins.input = real_input

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    started_at = time.time()
    bar = st.progress(0.0)
    status = st.empty()
    started = time.time()

    while thread.is_alive():
        with lock:
            frac, label = state["frac"], state["label"]
        _render_status(bar, status, frac, label, time.time() - started)
        time.sleep(0.4)
    thread.join(timeout=5)

    with lock:
        frac, label = state["frac"], state["label"]
    _render_status(bar, status, max(frac, 1.0), label, time.time() - started)
    time.sleep(0.4)
    bar.empty()
    status.empty()

    return buf.getvalue(), state["err"]


def _render_status(bar, slot, frac: float, label: str, elapsed: float) -> None:
    bar.progress(min(1.0, frac))
    mins, secs = divmod(int(elapsed), 60)
    remaining = ""
    if 0.02 < frac < 1.0:
        left = int(elapsed / frac - elapsed)
        remaining = f" · about {left // 60}m {left % 60}s left"
    slot.markdown(
        f'<div class="jf-status"><span class="jf-pulse"></span>'
        f"<b>{frac * 100:.0f}%</b> &nbsp;{label}"
        f'<span class="jf-elapsed">{mins}m {secs}s elapsed{remaining}</span></div>',
        unsafe_allow_html=True,
    )


def chip(label: str, ok: bool) -> str:
    return f'<span class="jf-chip {"jf-ok" if ok else "jf-no"}">{label}</span>'


# ------------------------------------------------------------- data panel

with st.sidebar:
    st.markdown("### Data sources")
    st.caption("Downloaded into `data/` on demand. Nothing is bundled.")

    reg = registry(data_stamp())
    avail = reg.availability()
    has_scopus = scopus_file() is not None

    from journal_traffic.scimago import ScimagoIndex

    sj = ScimagoIndex()
    has_sj = bool(sj.by_issn)

    st.markdown(
        chip("Scopus", has_scopus) + chip("SCImago", has_sj)
        + chip("PubMed", avail["pubmed"]) + chip("ABDC", avail["abdc"]),
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns(2)
    fetch = col_a.button("Fetch data", use_container_width=True)
    refresh = col_b.button("Force refresh", use_container_width=True)

    if fetch or refresh:
        from journal_traffic import datasets

        with st.spinner("Downloading datasets..."):
            results = datasets.fetch_all(DATA, force=refresh)
        for r in results:
            (st.success if r.ok else st.warning)(f"**{r.key}** — {r.message}")
        st.cache_resource.clear()
        st.cache_data.clear()

    with st.expander("What each list is for"):
        st.markdown(
            "- **Scopus** — coverage status; a discontinued title is "
            "disqualifying.\n"
            "- **SCImago** — real SJR quartiles per subject category.\n"
            "- **PubMed** — NLM's own serials file, authoritative for "
            "PubMed/MEDLINE presence.\n"
            "- **ABDC** — Journal Quality List ratings (A*, A, B, C) for "
            "business and management work."
        )

    for line in reg.summaries():
        st.caption(line)


# -------------------------------------------------------------------- UI

theme.hero("Where should this paper go?",
           "Paste a manuscript title. The tool derives its subject terms, "
           "pulls indexed journals in those fields, and ranks them by "
           "acceptance odds, measured review turnaround and remaining capacity.")

if not has_scopus:
    st.info(
        "No Scopus source list in `data/`. Coverage cannot be verified, so "
        "requiring Scopus will return nothing — use **Fetch data** in the "
        "sidebar, or drop Scopus from the index requirement."
    )

title = st.text_area("Manuscript title", height=88, key="title",
                     value=st.session_state.get("finder_title", ""))

terms_selected: list[str] = []
if title.strip():
    with st.spinner("Reading the literature to work out the subject "
                    "(a few seconds the first time; cached after that)..."):
        try:
            found = detect_terms(title.strip())
        except Exception as exc:                    # noqa: BLE001
            found = []
            st.error(f"Term detection failed: {type(exc).__name__}: {exc}")

    if found:
        def _label(t: dict) -> str:
            if t.get("note", "").startswith("unverified"):
                return f"{t['text']}  ·  count unavailable"
            tail = f" · {t['note']}" if t.get("note") else ""
            return f"{t['text']}  ·  {t['works']:,} papers{tail}"

        suggested_only = all(t.get("source") == "suggested" for t in found)
        if suggested_only:
            st.info(
                "No phrase in this title retrieves literature on its own — "
                "usually a coined name or an acronym string. These are the "
                "closest subjects the index does know about; keep the ones "
                "that describe your paper."
            )

        labels = {_label(t): t["text"] for t in found}
        picked = st.multiselect(
            "Subject terms detected from the title",
            options=list(labels),
            default=list(labels)[:1] if suggested_only else list(labels)[:3],
            help="Each term is searched separately. Journals matching more "
                 "than one are the best scope fit.",
        )
        terms_selected = [labels[p] for p in picked]
    else:
        st.info(
            "Nothing came back for this title, and the index had no near "
            "subject to suggest either. Type the field you would search for "
            "yourself."
        )
        manual = st.text_input("Subject to search instead")
        if manual.strip():
            terms_selected = [manual.strip()]

st.markdown("")

c1, c2, c3, c4 = st.columns(4, gap="medium")

with c1:
    st.markdown('<div class="jf-card"><h4>Must be listed in</h4>',
                unsafe_allow_html=True)
    index_choice = st.multiselect(
        "Indexes required",
        options=["Scopus", "PubMed", "ABDC"],
        default=["Scopus"],
        help="A journal must appear in every index you pick. Choose one for "
             "'ABDC journals only', or several to require all of them at once.",
        label_visibility="collapsed",
    )
    require_scopus = "Scopus" in index_choice
    require_pubmed = "PubMed" in index_choice
    require_abdc = "ABDC" in index_choice

    if require_pubmed and not avail["pubmed"]:
        st.caption("PubMed list not downloaded — fetch it in the sidebar.")
    if require_abdc and not avail["abdc"]:
        st.caption("ABDC list not downloaded — fetch it in the sidebar.")
    if not index_choice:
        st.caption("No index required — results will include journals whose "
                   "coverage was never confirmed.")
    st.markdown("</div>", unsafe_allow_html=True)

with c2:
    st.markdown('<div class="jf-card"><h4>Ranking lists</h4>', unsafe_allow_html=True)
    abdc_ranks = st.multiselect(
        "ABDC rating", ["A*", "A", "B", "C"], default=[],
        disabled=not require_abdc,
        help="Narrows the ABDC requirement to these ratings. Empty means any "
             "rated journal counts." if require_abdc else
             "Requires ABDC above.",
    )
    quartiles = st.multiselect(
        "SJR quartile", [1, 2, 3, 4], default=[],
        format_func=lambda q: f"Q{q}",
        help="Real SJR quartiles only. Journals SCImago does not cover are "
             "excluded rather than ranked by a within-batch proxy.",
    )
    st.markdown("</div>", unsafe_allow_html=True)

with c3:
    st.markdown('<div class="jf-card"><h4>Search depth</h4>', unsafe_allow_html=True)
    limit = st.number_input("Journals per term", 5, 500, 40, step=5)
    top_n = st.number_input("Shortlist size", 3, 50, 10)
    max_pub = st.number_input("Max per publisher", 0, 20, 3,
                              help="0 disables the cap.")
    fast_only = st.checkbox(
        "Fast acceptance only", value=False,
        help="Keeps journals whose submission-to-acceptance time is known and "
             "within the limit below. Measured from Crossref dates where "
             "available, otherwise the journal's own stated figure. Journals "
             "with no known time, or a suspiciously fast one (under 14 days), "
             "are left out.")
    fast_days = st.number_input("Max days to acceptance", 14, 365, 90, step=15,
                                disabled=not fast_only)
    sort_by = st.radio("Rank by", ["fit", "speed"], horizontal=True,
                       format_func=lambda x: "Best fit" if x == "fit"
                       else "Fastest acceptance")
    st.markdown("</div>", unsafe_allow_html=True)

with c4:
    st.markdown('<div class="jf-card"><h4>Evidence gathering</h4>', unsafe_allow_html=True)
    scrape = st.checkbox("Declared acceptance rates", value=True,
                         help="Reads publisher pages. Slow, and finds few.")
    turnaround = st.checkbox("Measured review turnaround", value=True,
                             help="Crossref received-to-accepted dates. Needed "
                                  "for fast-acceptance results.")
    semantic = st.checkbox("Subject matching", value=True,
                           help="Compares each journal's published titles "
                                "against the manuscript.")
    special = st.checkbox("Special-issue composition", value=False,
                          help="Slowest step. Off by default.")
    fetch_apc = st.checkbox("APC charges", value=True,
                            help="Article processing charge from DOAJ, then "
                                 "OpenAlex, then the publisher's page. Each "
                                 "figure shows its source.")
    max_apc = st.number_input(
        "Max APC (USD, 0 = no limit)", 0, 20000, 0, step=250,
        disabled=not fetch_apc,
        help="Removes journals whose known charge exceeds this. No-APC, "
             "subscription and hybrid journals (free route) are kept; so are "
             "journals whose charge could not be found — check those.")
    free_only = st.checkbox(
        "Only journals with a free option", value=False, disabled=not fetch_apc,
        help="Keeps diamond OA (no APC), subscription and hybrid journals "
             "(publish free, open access optional), and OA journals that "
             "offer APC waivers.")
    st.markdown("</div>", unsafe_allow_html=True)

st.markdown("")

n_terms = max(1, len(terms_selected))
est = max(1, int(limit * n_terms * 0.06))
lc, rc = st.columns([3, 1], gap="medium")
with lc:
    go = st.button("Find journals", type="primary", use_container_width=True)
with rc:
    st.caption(f"≈ {est}–{est * 2} min · {n_terms} term(s) × {limit} journals")

if go:
    if not title.strip():
        st.warning("Enter a manuscript title.")
    elif not terms_selected:
        st.warning("Keep at least one detected subject term.")
    else:
        args = Args(
            title=title.split(),
            term=terms_selected,
            limit=int(limit),
            top=int(top_n),
            fast=not special,
            no_scrape=not scrape,
            no_turnaround=not turnaround,
            no_semantic=not semantic,
            max_per_publisher=int(max_pub),
            quartile=quartiles,
            require_scopus=require_scopus,
            require_pubmed=require_pubmed,
            abdc_ranks=(abdc_ranks or ["A*", "A", "B", "C"]) if require_abdc
                       else None,
            no_apc=not fetch_apc,
            max_apc_usd=(int(max_apc) if fetch_apc and max_apc else None),
            free_only=bool(fetch_apc and free_only),
            fast_days=int(fast_days) if fast_only else None,
            sort_by=sort_by,
        )
        run_started = time.time()
        output, err = run_pipeline(args)
        st.session_state["log"] = output
        st.session_state["err"] = err
        st.session_state["run_started"] = run_started

if "log" in st.session_state:
    output = st.session_state["log"]
    err = st.session_state.get("err")
    if err:
        st.error(f"Run failed: {type(err).__name__}: {err}")

    shortlist, allj = OUT / "shortlist.csv", OUT / "all_journals.csv"

    # Only show a file this run wrote. Otherwise a search that matched
    # nothing would display the previous run's shortlist as its result.
    started = st.session_state.get("run_started", 0)
    fresh = shortlist.exists() and shortlist.stat().st_mtime >= started - 1

    if fresh:
        df = pd.read_csv(shortlist)

        # Show where the quartile came from. A proxy quartile is a within-batch
        # citation percentile, not an SJR quartile, and must never be read as
        # one — it only ever appears when no quartile filter was applied.
        if "quartile" in df.columns:
            proxy = df.get("quartile_is_proxy", False)
            proxy = proxy.fillna(False).astype(bool) if hasattr(proxy, "fillna") \
                else pd.Series(False, index=df.index)
            df["quartile_label"] = [
                "—" if pd.isna(q) else f"Q{int(q)}{' (proxy)' if p else ''}"
                for q, p in zip(df["quartile"], proxy)
            ]

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Shortlisted", len(df))
        if "evidence_tier" in df.columns:
            hard = int(df["evidence_tier"].isin(["A", "B"]).sum())
            m2.metric("Declared-rate evidence", f"{hard}/{len(df)}")
        if "acceptance_days" in df.columns:
            acc = pd.to_numeric(df["acceptance_days"], errors="coerce")
            n_fast = int(df["acceptance_speed"].isin(["Very fast", "Fast"]).sum())
            m3.metric("Fast acceptance", f"{n_fast}/{len(df)}",
                      help="Known acceptance within 90 days. Median where known: "
                           + ("—" if acc.dropna().empty else f"{acc.median():.0f} d"))
        elif "review_days_median" in df.columns:
            med = pd.to_numeric(df["review_days_median"], errors="coerce").median()
            m3.metric("Median review", "—" if pd.isna(med) else f"{med:.0f} d")
        if "apc_usd_est" in df.columns:
            usd = pd.to_numeric(df["apc_usd_est"], errors="coerce")
            priced = usd[usd > 0]
            n_free = int(df.get("free_option", pd.Series(dtype=str)).eq("Yes").sum())
            m4.metric("Free option", f"{n_free}/{len(df)}",
                      help="Journals you can publish in without paying. "
                           + ("Median APC where charged: "
                              f"${priced.median():,.0f}" if not priced.empty else ""))
        elif "term_matches" in df.columns:
            m4.metric("Multi-term fits", int((df["term_matches"] > 1).sum()))

        # Stored as a fraction; the column renders a percent, so scale it here
        # rather than showing every journal as "0%".
        if "acceptance_rate_used" in df.columns:
            df["acceptance_rate_used"] = (
                pd.to_numeric(df["acceptance_rate_used"], errors="coerce") * 100
            )

        if "acceptance_days" in df.columns:
            from journal_traffic.speed import display as speed_display
            df["acceptance_time"] = [speed_display(r) for r in df.to_dict("records")]
        show = [c for c in [
            "display_name", "publisher_group", "quartile_label",
            "abdc_rank", "in_pubmed", "opportunity_score",
            "apc", "free_option", "publishing_model", "apc_source",
            "acceptance_time", "speed_source", "days_to_publication",
            "acceptance_rate_used", "evidence_tier",
            "submissions_now_est", "regular_track_odds", "articles_latest",
            "quality_flags",
        ] if c in df.columns]

        st.dataframe(
            df[show],
            use_container_width=True,
            hide_index=True,
            column_config={
                "display_name": st.column_config.TextColumn("Journal", width="large"),
                "publisher_group": "Publisher",
                "quartile_label": "SJR Q",
                "abdc_rank": "ABDC",
                "in_pubmed": st.column_config.CheckboxColumn("PubMed"),
                "apc": st.column_config.TextColumn(
                    "APC", help="Article processing charge. Non-USD amounts show "
                                "an approximate USD figure. 'Subscription' means no "
                                "mandatory charge; an optional OA fee may apply."),
                "acceptance_time": st.column_config.TextColumn(
                    "Acceptance time",
                    help="Submission to acceptance. Very fast <= 45 d, Fast <= 90 d, "
                         "Moderate <= 180 d. 'Suspiciously fast' (< 14 d measured) "
                         "is a warning, not a plus."),
                "speed_source": st.column_config.TextColumn(
                    "Speed source",
                    help="Measured = median of recent articles' Crossref "
                         "received/accepted dates. Stated = the journal's own figure."),
                "days_to_publication": st.column_config.NumberColumn(
                    "To publication", format="%.0f d",
                    help="Submission to online publication, where known"),
                "free_option": st.column_config.TextColumn(
                    "Free option",
                    help="Yes = you can publish without paying (no APC, or the "
                         "subscription route of a hybrid journal). 'Waiver on "
                         "request' = APC journal that grants waivers."),
                "publishing_model": st.column_config.TextColumn(
                    "Model", help="Diamond OA (free OA), Gold OA (APC required), "
                                  "Hybrid (free, OA optional), Subscription"),
                "apc_source": st.column_config.TextColumn(
                    "APC source", help="DOAJ is the journal's own declaration; "
                                       "'Publisher page' is read from the website "
                                       "and should be verified."),
                "opportunity_score": st.column_config.ProgressColumn(
                    "Opportunity", min_value=0, max_value=100, format="%.0f"),
                "acceptance_rate_used": st.column_config.NumberColumn(
                    "Accept.", format="%.0f%%",
                    help="Estimated acceptance rate. Tier D means it comes "
                         "from a quartile prior, not a declared figure."),
                "evidence_tier": "Tier",
                "review_days_median": st.column_config.NumberColumn(
                    "Review", format="%.0f d"),
                "submissions_now_est": st.column_config.NumberColumn(
                    "Submissions/yr", format="%.0f"),
                "articles_latest": st.column_config.NumberColumn("Articles/yr"),
                "quality_flags": st.column_config.TextColumn("Flags", width="medium"),
            },
        )

        d1, d2 = st.columns(2)
        d1.download_button("Download shortlist (CSV)", shortlist.read_bytes(),
                           "shortlist.csv", "text/csv", use_container_width=True)
        if allj.exists():
            d2.download_button("Download all journals (CSV)", allj.read_bytes(),
                               "all_journals.csv", "text/csv",
                               use_container_width=True)
    else:
        if allj.exists() and allj.stat().st_mtime >= started - 1:
            n = len(pd.read_csv(allj))
            st.warning(
                f"No journal matched your filters. {n} were found and scored — "
                "loosen the quartile, index or ABDC requirement, or download "
                "the full set below."
            )
            st.download_button("Download all journals (CSV)", allj.read_bytes(),
                               "all_journals.csv", "text/csv")
        else:
            st.info("No shortlist was produced — the log below says why.")

    with st.expander("Run log", expanded=not shortlist.exists()):
        st.code(output or "(no output)", language="text")

st.divider()
st.caption(
    "Scores are relative to the journals in this batch, not absolute. Check the "
    "evidence tier: A/B rest on declared acceptance rates, C on the model, D on "
    "a quartile prior. ABDC ratings cover business and management only, and "
    "Scopus coverage reflects the source list in data/ — refresh it before "
    "quoting indexing status to a client."
)
