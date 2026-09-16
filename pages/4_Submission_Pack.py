"""Submission Pack — every file a journal asks for, in one click."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Submission Pack · CS Software Solutions",
                   page_icon="📦", layout="wide")

from core import theme, nav, client, services as svc_mod   # noqa: E402

svc_mod.init()
theme.header("Submission Pack", "Everything the journal asks for, ready to upload")
nav.bar("pages/4_Submission_Pack.py")
theme.sidebar()
client.panel()

import os                                                   # noqa: E402
import tempfile                                             # noqa: E402

import pandas as pd                                         # noqa: E402

from core import ingest, profiles, discover, submission_pack as sp   # noqa: E402

st.caption("Reads the journal's author guidelines, lists what it requires at "
           "submission, and builds the files: formatted and blinded manuscripts, "
           "title page, cover letter, highlights, declarations, separate figures "
           "and tables, reviewer template and a checklist. Nothing is invented — "
           "anything only the authors can supply is marked [placeholder] or [confirm].")

c1, c2 = st.columns(2)
with c1:
    journal = st.text_input("Target journal", placeholder="e.g. Expert Systems with Applications")
    guide_url = st.text_input("Author guidelines URL (optional)",
                              help="Leave empty to look the journal up automatically. "
                                   "Paste the 'Guide for authors' link for best results.")
with c2:
    up = st.file_uploader("Manuscript", type=["docx", "tex", "zip", "pdf"])
    with st.expander("Guidelines blocked? Paste the text instead"):
        pasted = st.text_area("Author guidelines text", height=160,
                              help="Some publisher sites block automated reading. "
                                   "Copy the guide-for-authors page and paste it here.")

all_profiles = profiles.load_all()
names = [p["name"] for p in all_profiles]
best, _ = profiles.resolve(journal) if journal.strip() else (None, [])
fmt = st.selectbox("Format the manuscript as",
                   ["Auto (from journal / publisher)"] + names,
                   help="Auto uses a matching journal profile, or the publisher's "
                        "base layout read from the guidelines.")

go = st.button("📦 Build submission package", type="primary",
               use_container_width=True, disabled=not (up and (journal or guide_url or pasted)))

if go:
    with st.spinner("Reading the manuscript…"):
        tmp = tempfile.mkdtemp(prefix="sp_")
        path = os.path.join(tmp, up.name)
        with open(path, "wb") as fh:
            fh.write(up.getbuffer())
        ms = ingest.ingest(path, image_dir=os.path.join(tmp, "img"))
    with st.spinner("Reading the journal's author guidelines…"):
        g = sp.gather_guidelines(journal.strip(), guide_url.strip() or None, pasted)
    if fmt != "Auto (from journal / publisher)":
        prof = next(p for p in all_profiles if p["name"] == fmt)
    elif best is not None:
        prof = best
    else:
        base_id, label = discover.detect_publisher(" ".join(g.pages), g.text)
        spec = {k: v for k, v in discover.extract_spec(g.text).items() if v is not None}
        prof = discover.build_profile(g.journal or journal or "Target journal",
                                      guide_url or (g.homepage or ""), spec, base_id,
                                      g.publisher or label, [])
    with st.spinner("Building the files…"):
        zbytes, reqs = sp.build_pack(ms, prof, g)
    st.session_state["sp_result"] = (zbytes, reqs, g, prof["name"], ms.title)

if "sp_result" in st.session_state:
    zbytes, reqs, g, prof_name, title = st.session_state["sp_result"]
    if not g.text.strip():
        st.error("No guidelines could be read. The files below use general journal "
                 "conventions — paste the guidelines text for journal-specific results.")
    elif g.errors:
        st.warning("Some pages could not be read: " + "; ".join(g.errors[:3]))
    req_n = sum(1 for r in reqs if r.status == sp.REQUIRED)
    done = sum(1 for r in reqs if r.status != sp.NOT_FOUND and r.output)
    todo = sum(1 for r in reqs if r.status != sp.NOT_FOUND and not r.output)
    m1, m2, m3 = st.columns(3)
    m1.metric("Required items found", req_n)
    m2.metric("Files generated for them", done)
    m3.metric("Need your action", todo)
    st.caption(f"Journal: **{g.journal or '—'}** · formatted as **{prof_name}** · "
               f"guidelines read from {len(g.pages)} page(s)")

    df = pd.DataFrame([{
        "Item": r.label, "Status": r.status, "Detail": r.detail,
        "File": r.output, "To do": r.action, "Guidelines say": r.evidence,
    } for r in reqs])
    order = {sp.REQUIRED: 0, sp.OPTIONAL: 1, sp.NOT_FOUND: 2}
    df = df.sort_values("Status", key=lambda s: s.map(order))
    show_all = st.toggle("Show items not mentioned in the guidelines", value=False)
    view = df if show_all else df[df["Status"] != sp.NOT_FOUND]
    st.dataframe(view, use_container_width=True, hide_index=True,
                 column_config={"Guidelines say": st.column_config.TextColumn(width="large")})

    safe = "".join(ch if ch.isalnum() else "_" for ch in (g.journal or "journal"))[:40]
    st.download_button("⬇️ Download submission package (.zip)", zbytes,
                       f"Submission_{safe}.zip", "application/zip",
                       type="primary", use_container_width=True)
    if g.pages:
        with st.expander("Guideline pages read"):
            for p_ in g.pages:
                st.markdown(f"- {p_}" if not p_.startswith("http") else f"- [{p_}]({p_})")
