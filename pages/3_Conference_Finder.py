"""Conference Finder — which conferences fit this paper, and which are open now."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Conference Finder · CS Software Solutions",
                   page_icon="🎤", layout="wide")

from core import theme, nav, client, services as svc_mod   # noqa: E402

svc_mod.init()
theme.header("Conference Finder", "Which conferences suit this paper?")
nav.bar("pages/3_Conference_Finder.py")
theme.sidebar()
client.panel()

import io                                                   # noqa: E402
from contextlib import redirect_stdout                      # noqa: E402

import pandas as pd                                         # noqa: E402


@st.cache_data(show_spinner=False, ttl=3600)
def detect_terms(title: str) -> list[str]:
    from journal_traffic.sources import OpenAlexClient
    from journal_traffic.terms import terms_from_title
    return [t.text for t in terms_from_title(OpenAlexClient(), title)][:6]


@st.cache_resource(show_spinner=False)
def scopus_proceedings():
    from journal_traffic.conferences import ScopusProceedings
    return ScopusProceedings()


with st.sidebar:
    st.markdown("### Conference data")
    sp = scopus_proceedings()
    st.caption(sp.summary())
    from journal_traffic.conferences import CoreRanking
    core = CoreRanking(fetch=False)
    st.caption(core.summary())
    if not core.available and st.button("Download CORE ranking"):
        with st.spinner("Fetching the CORE portal export…"):
            from pathlib import Path
            ok = CoreRanking.download(Path("data") / CoreRanking.FILE.name)
        (st.success if ok else st.error)(
            "CORE ranking saved." if ok else
            "Download failed. Export the CSV from portal.core.edu.au/conf-ranks "
            "and save it as data/core_conference_ranks.csv.")

title = st.text_area("Paper title", height=80,
                     placeholder="e.g. Mobility-aware deep reinforcement learning "
                                 "clustering for wireless sensor networks")

terms: list[str] = []
if title.strip():
    with st.spinner("Working out what the paper is about…"):
        try:
            found = detect_terms(title.strip())
        except Exception as exc:  # noqa: BLE001
            found = []
            st.warning(f"Term detection failed ({type(exc).__name__}); add terms below.")
    terms = st.multiselect("Search terms", options=found, default=found[:4],
                           help="Detected from the title. Remove any that are too broad.")
    extra = st.text_input("Add a term (optional)")
    if extra.strip():
        terms.append(extra.strip())

c1, c2, c3 = st.columns(3)
with c1:
    require_scopus = st.checkbox("Scopus-indexed proceedings only", value=False)
    st.caption("Conferences whose submission deadline has already passed are "
               "left out automatically.")
with c2:
    min_rank = st.selectbox("Minimum CORE rank", ["Any", "A*", "A", "B", "C"],
                            help="CORE ranks cover computing conferences mainly.")
with c3:
    limit = st.number_input("Shortlist size", 5, 60, 20)

if st.button("Find conferences", type="primary", use_container_width=True,
             disabled=not terms):
    from journal_traffic.conferences import find_conferences, upcoming_calls
    log = io.StringIO()
    with st.spinner("Searching OpenAlex, Scopus proceedings, CORE and WikiCFP…"):
        with redirect_stdout(log):
            try:
                confs = find_conferences(
                    title, terms=terms, limit=int(limit),
                    require_scopus=require_scopus, upcoming_only=False,
                    min_rank=None if min_rank == "Any" else min_rank)
                calls = upcoming_calls(terms)
                err = None
            except Exception as exc:  # noqa: BLE001
                confs, calls, err = [], [], exc
    st.session_state["conf_result"] = (confs, calls, log.getvalue(), err)

if "conf_result" in st.session_state:
    confs, calls, log_text, err = st.session_state["conf_result"]
    if err:
        st.error(f"Search failed: {type(err).__name__}: {err}")
    open_c = [c for c in confs if c.status in ("Open", "Closing soon")]
    later = [c for c in confs if c.status == "No call announced"]
    if open_c:
        # soonest deadline first within the strongest fits: best chances on top
        open_c.sort(key=lambda c: (-round(c.score / 10), c.days_to_deadline))
        df = pd.DataFrame([c.row() for c in open_c])
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Open to submit", len(df))
        m2.metric("Closing within 14 days", int((df["status"] == "Closing soon").sum()))
        m3.metric("Scopus proceedings", int((df["scopus_indexed"] == "Yes").sum()))
        m4.metric("CORE ranked", int(df["core_rank"].notna().sum()))
        show = ["status", "name", "acronym", "next_deadline", "days_to_deadline",
                "abstract_deadline", "notification", "next_when", "next_where",
                "website", "cfp_url", "scopus_indexed", "core_rank", "publisher",
                "papers_on_topic", "flags", "score"]
        st.dataframe(
            df[show], use_container_width=True, hide_index=True,
            column_config={
                "status": st.column_config.TextColumn(
                    "Status", help="Open = deadline ahead; Closing soon = 14 days or less"),
                "name": st.column_config.TextColumn("Conference", width="large"),
                "acronym": "Acronym",
                "next_deadline": st.column_config.TextColumn(
                    "Paper due date", help="Full paper submission deadline"),
                "days_to_deadline": st.column_config.NumberColumn("Days left"),
                "abstract_deadline": "Abstract due",
                "notification": "Notification",
                "next_when": "Conference dates", "next_where": "Location",
                "website": st.column_config.LinkColumn(
                    "Conference website", display_text="Open site"),
                "cfp_url": st.column_config.LinkColumn(
                    "Call for papers", display_text="WikiCFP"),
                "scopus_indexed": "Scopus", "core_rank": "CORE",
                "publisher": "Publisher",
                "papers_on_topic": st.column_config.NumberColumn(
                    "On-topic papers", help="Papers on your terms in the last 5 years"),
                "flags": st.column_config.TextColumn("Flags", width="medium"),
                "score": st.column_config.ProgressColumn(
                    "Fit", min_value=0, max_value=100, format="%.0f"),
            })
        st.download_button("Download open conferences (CSV)",
                           df.to_csv(index=False).encode(), "open_conferences.csv",
                           "text/csv", use_container_width=True)
        for c in open_c[:5]:
            site = f"[conference website]({c.website})" if c.website else "no website listed"
            cfp = f" · [call for papers]({c.cfp_url})" if c.cfp_url else ""
            st.markdown(
                f"**{c.acronym or c.name}** — due **{c.next_deadline}** "
                f"({c.days_to_deadline} days left) · {c.next_where or 'location TBA'} · "
                f"{site}{cfp}")
    elif not err:
        st.info("No matching conference has an open call right now. Relevant "
                "conferences that have not announced their next deadline are "
                "listed below — check back on their websites.")

    if later:
        ldf = pd.DataFrame([c.row() for c in later])
        with st.expander(f"Relevant conferences with no call announced yet ({len(later)})"):
            st.dataframe(
                ldf[["name", "acronym", "publisher", "last_year", "scopus_indexed",
                     "core_rank", "website", "papers_on_topic"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "name": st.column_config.TextColumn("Conference", width="large"),
                    "last_year": st.column_config.NumberColumn("Last held", format="%d"),
                    "website": st.column_config.LinkColumn(
                        "Conference website", display_text="Open site"),
                })
            st.caption("No deadline was found for the next edition, so these are not "
                       "counted as open. Their websites usually post the call a few "
                       "months ahead.")

    if calls:
        with st.expander(f"All open calls for these terms ({len(calls)}) — "
                         "includes new conferences with no proceedings yet"):
            cdf = pd.DataFrame([vars(c) for c in calls])
            st.dataframe(cdf[["acronym", "name", "deadline", "notification", "when",
                              "where", "website", "url"]],
                         use_container_width=True, hide_index=True,
                         column_config={
                             "deadline": "Paper due date",
                             "website": st.column_config.LinkColumn(
                                 "Conference website", display_text="Open site"),
                             "url": st.column_config.LinkColumn(
                                 "Call for papers", display_text="WikiCFP")})
            st.caption("Straight from WikiCFP and not checked for indexing or "
                       "quality — verify the organiser before recommending one.")

    with st.expander("Run log"):
        st.code(log_text or "(no output)", language="text")

st.divider()
st.caption("Relevance comes from OpenAlex proceedings on your terms; Scopus status "
           "from the proceedings sheets of the Scopus source list in data/; ranks "
           "from the CORE portal; upcoming editions from WikiCFP. Always confirm "
           "dates and indexing on the conference's own site before submitting.")
