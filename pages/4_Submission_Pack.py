"""Submission Pack — format the manuscript and build every file the journal asks for."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Submission Pack · CS Software Solutions",
                   page_icon="📦", layout="wide")

from core import theme, nav, client, services as svc_mod   # noqa: E402

svc_mod.init()
theme.header("Submission Pack", "Formatted manuscript + every submission file, in one click")
nav.bar("pages/4_Submission_Pack.py")
theme.sidebar()
client.panel()

import io                                                   # noqa: E402
import os                                                   # noqa: E402
import tempfile                                             # noqa: E402
import zipfile                                              # noqa: E402

import pandas as pd                                         # noqa: E402
import streamlit.components.v1 as components               # noqa: E402

from core import (ingest, profiles, discover, template_probe,  # noqa: E402
                  submission_pack as sp)

ss = st.session_state

# --------------------------------------------------------------------------- #
# 1. manuscript, journal, template
# --------------------------------------------------------------------------- #
st.subheader("1. Manuscript and journal")
c1, c2 = st.columns(2)
with c1:
    up = st.file_uploader("Manuscript", type=["docx", "tex", "zip", "pdf"])
    journal = st.text_input("Target journal",
                            placeholder="e.g. Expert Systems with Applications")
    guide_url = st.text_input("Author guidelines URL (optional)",
                              help="Empty = found automatically. The 'Guide for "
                                   "authors' link gives the best results.")
with c2:
    tpl_up = st.file_uploader("Journal template (optional)",
                              type=["docx", "dotx", "zip", "cls"],
                              help="The publisher's Word template and/or LaTeX kit — "
                                   "the most accurate formatting.")
    with st.expander("Guidelines blocked? Paste the text instead"):
        pasted = st.text_area("Author guidelines text", height=140)

tpl_bytes, tex_extras = None, {}
if tpl_up:
    raw, low = tpl_up.getbuffer().tobytes(), tpl_up.name.lower()
    if low.endswith((".docx", ".dotx")):
        tpl_bytes = raw
    elif low.endswith(".cls"):
        tex_extras[tpl_up.name] = raw
    elif low.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for n in zf.namelist():
                nl = n.lower()
                if nl.endswith((".cls", ".sty", ".bst", ".bib")):
                    tex_extras[os.path.basename(n)] = zf.read(n)
                elif nl.endswith((".docx", ".dotx")) and tpl_bytes is None:
                    tpl_bytes = zf.read(n)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read that kit: {exc}")

# --------------------------------------------------------------------------- #
# 2. formatting
# --------------------------------------------------------------------------- #
st.subheader("2. Formatting")
all_profiles = profiles.load_all()
names = [p["name"] for p in all_profiles]
AUTO, TEMPLATE = "Auto (journal / publisher from guidelines)", "Use my uploaded template"
choices = ([TEMPLATE] if (tpl_bytes or tex_extras) else []) + [AUTO] + names
default = 0 if (tpl_bytes or tex_extras) else choices.index(AUTO)
fmt_choice = st.selectbox(
    "Format the manuscript as", choices, index=default,
    help="Auto finds the journal's real publisher (OpenAlex / guidelines page) and "
         "applies that journal's own limits from its guidelines. Pick a profile to "
         "force a format; upload a template for the exact layout.")

f1, f2, f3 = st.columns(3)
with f1:
    eq_mode = st.radio("Equations", ["native", "image"], horizontal=True,
                       format_func=lambda x: "Editable Word equations" if x == "native"
                       else "Images")
    keep_masthead = True
    if tpl_bytes:
        inv = template_probe.inventory(tpl_bytes)
        keep_masthead = st.checkbox(f"Keep template logo/masthead ({inv['logos']} image(s))",
                                    value=True)
with f2:
    float_mode = st.radio("LaTeX figures & tables", ["journal", "inline"], horizontal=True,
                          format_func=lambda x: "Float (journal style)" if x == "journal"
                          else "Pin in place")
    compact_layout = st.checkbox("Compact LaTeX layout", value=False)
with f3:
    add_mentions = st.checkbox("Add missing in-text mentions", value=True,
                               help="Adds 'as shown in Fig. 3' for any figure or table "
                                    "the text never refers to.")
    want_latex = st.checkbox("Include LaTeX project", value=True)
    want_pdf = st.checkbox("Include PDF of the manuscript", value=True,
                           help="Needs LibreOffice on the server.")

go = st.button("📦 Format & build submission package", type="primary",
               use_container_width=True,
               disabled=not (up and (journal or guide_url or pasted or tpl_up)))


def resolve_profile(g, choice):
    if choice == TEMPLATE:
        prof, _spec = discover.profile_from_template(
            tpl_up.name, tpl_bytes or next(iter(tex_extras.values())))
        return prof, f"your uploaded template ({tpl_up.name})"
    if choice != AUTO:
        pid = next(p["id"] for p in all_profiles if p["name"] == choice)
        return sp.choose_profile(journal, g, explicit_id=pid)
    return sp.choose_profile(journal.strip(), g)


def run_build(ms, g, choice, prog=None):
    prof, why = resolve_profile(g, choice)
    if prog:
        prog.progress(0.45, f"Formatting as {prof['name']}…")
    opt = sp.FormatOptions(template_bytes=tpl_bytes if choice == TEMPLATE else None,
                           tex_extras=tex_extras if choice == TEMPLATE else {},
                           equation_mode=eq_mode, keep_masthead=keep_masthead,
                           float_mode=float_mode, compact=compact_layout,
                           add_missing_mentions=add_mentions,
                           include_latex=want_latex, include_pdf=want_pdf)
    formatted = sp.format_manuscript(ms, prof, opt)
    if prog:
        prog.progress(0.8, "Building the submission files…")
    zbytes, reqs = sp.build_pack(ms, prof, g, fmt=opt, formatted=formatted)
    ss["sp_result"] = dict(zip=zbytes, reqs=reqs, g=g, prof=prof["name"], why=why,
                           choice=choice,
                           formatted={k: formatted[k] for k in
                                      ("docx", "zip", "pdf", "html", "report",
                                       "applied", "warning")})


if go:
    prog = st.progress(0.0, "Reading the manuscript…")
    tmp = tempfile.mkdtemp(prefix="sp_")
    path = os.path.join(tmp, up.name)
    with open(path, "wb") as fh:
        fh.write(up.getbuffer())
    ms = ingest.ingest(path, image_dir=os.path.join(tmp, "img"))
    prog.progress(0.2, "Reading the journal's author guidelines…")
    g = sp.gather_guidelines(journal.strip(), guide_url.strip() or None, pasted)
    ss["sp_ms"], ss["sp_g"] = ms, g
    run_build(ms, g, fmt_choice, prog)
    prog.progress(1.0, "Done")
    prog.empty()

# --------------------------------------------------------------------------- #
# 3. results
# --------------------------------------------------------------------------- #
if "sp_result" in ss:
    R = ss["sp_result"]
    reqs, g, F = R["reqs"], R["g"], R["formatted"]
    st.subheader("3. Your submission package")
    if not g.text.strip():
        st.error("No guidelines could be read — files use general conventions. "
                 "Paste the guidelines text for journal-specific results.")
    elif g.errors:
        st.warning("Some pages could not be read: " + "; ".join(g.errors[:3]))
    if F.get("warning"):
        st.warning(F["warning"])

    fails = sum(1 for lvl, _ in F["report"] if lvl == "fail")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Required items", sum(1 for r in reqs if r.status == sp.REQUIRED))
    m2.metric("Files generated", sum(1 for r in reqs if r.status != sp.NOT_FOUND and r.output))
    m3.metric("Need your action", sum(1 for r in reqs if r.status != sp.NOT_FOUND and not r.output))
    m4.metric("Format check failures", fails)
    st.info(f"**Formatted as {R['prof']}** — {R['why']}.")
    rc1, rc2 = st.columns([3, 1])
    with rc1:
        again = st.selectbox("Not the right format? Re-format as", choices,
                             index=choices.index(R["choice"]) if R["choice"] in choices else 0,
                             key="sp_again")
    with rc2:
        st.write("")
        if st.button("Rebuild", use_container_width=True,
                     disabled=again == R["choice"] or "sp_ms" not in ss):
            with st.spinner(f"Re-formatting as {again}…"):
                run_build(ss["sp_ms"], ss["sp_g"], again)
            st.rerun()

    safe = "".join(ch if ch.isalnum() else "_" for ch in (g.journal or "journal"))[:40]
    st.download_button("⬇️ Download complete submission package (.zip)", R["zip"],
                       f"Submission_{safe}.zip", "application/zip",
                       type="primary", use_container_width=True)

    t_req, t_fmt, t_prev = st.tabs(["Submission requirements", "Format check",
                                    "Formatted manuscript"])
    with t_req:
        df = pd.DataFrame([{"Item": r.label, "Status": r.status, "Detail": r.detail,
                            "File": r.output, "To do": r.action,
                            "Guidelines say": r.evidence} for r in reqs])
        order = {sp.REQUIRED: 0, sp.OPTIONAL: 1, sp.NOT_FOUND: 2}
        df = df.sort_values("Status", key=lambda s: s.map(order))
        if not st.toggle("Show items not mentioned in the guidelines", value=False):
            df = df[df["Status"] != sp.NOT_FOUND]
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"Guidelines say": st.column_config.TextColumn(width="large")})
        if g.pages:
            with st.expander("Guideline pages read"):
                for p_ in g.pages:
                    st.markdown(f"- [{p_}]({p_})" if p_.startswith("http") else f"- {p_}")
    with t_fmt:
        for a in F.get("applied", []):
            st.info(f"Applied: {a}")
        shown = False
        for lvl, msg in F["report"]:
            if lvl in ("fail", "warn"):
                {"warn": st.warning, "fail": st.error}[lvl](msg)
                shown = True
        if not shown:
            st.success("The formatted manuscript passes every check for this journal.")
    with t_prev:
        d1, d2, d3 = st.columns(3)
        d1.download_button("Word (.docx)", F["docx"], "manuscript.docx",
                           use_container_width=True)
        if F.get("zip"):
            d2.download_button("LaTeX project (.zip)", F["zip"], "manuscript_latex.zip",
                               use_container_width=True)
        if F.get("pdf"):
            d3.download_button("PDF", F["pdf"], "manuscript.pdf",
                               use_container_width=True)
        components.html(F["html"], height=720, scrolling=True)
