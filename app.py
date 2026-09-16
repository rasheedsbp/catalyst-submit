"""Journal Formatting Tool.

Three steps: what journal, what manuscript, download. Everything optional lives
behind an expander so the main path stays a straight line.

Run:  streamlit run app.py
"""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from core import (profiles, ingest, checks, preview, discover,
                  template_probe, edits, proposals as prop_mod, xref, compact)
from core import theme, nav, client, services as svc_mod
from core import render_latex, render_docx
from core.model import Manuscript, Author

st.set_page_config(page_title="Journal Formatter", page_icon="📄",
                   layout="wide")

svc_mod.init()
theme.header("Journal Formatter", "Free formatting tool")
nav.bar("app.py")
theme.sidebar()
client.panel()

_greet = client.greeting()
if _greet:
    st.caption(_greet)

ss = st.session_state
ss.setdefault("built", None)
ss.setdefault("stamp", None)
ss.setdefault("obj_edits", {})
ss.setdefault("accepted", set())



# =========================================================================== #
# 1. Journal + files
# =========================================================================== #
st.subheader("1. Your manuscript")

all_profiles = profiles.load_all()
names = [p["name"] for p in all_profiles]

c1, c2 = st.columns(2)
with c1:
    up = st.file_uploader("Manuscript", type=["docx", "tex", "zip", "pdf", "txt"])
with c2:
    tpl_up = st.file_uploader("Journal template (optional)",
                              type=["docx", "dotx", "zip", "cls"],
                              help="The publisher's Word template and/or LaTeX "
                                   "kit. Gives the most accurate result and can "
                                   "stand in for choosing a journal.")

# --- unpack whatever the template upload contains ------------------------- #
tpl_bytes: bytes | None = None
tex_extras: dict[str, bytes] = {}
if tpl_up:
    raw = tpl_up.getbuffer().tobytes()
    low = tpl_up.name.lower()
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
        except Exception as exc:
            st.error(f"Could not read that kit: {exc}")

_usual = [j for j in client.journals()
          if any(j.lower() in n.lower() or n.lower() in j.lower() for n in names)]
_default = 0 if tpl_bytes else (
    next((names.index(n) + 1 for n in names
          for j in _usual if j.lower() in n.lower()), 1))
journal = st.selectbox(
    "Journal", ["— use my uploaded template —"] + names,
    index=_default,
    help="Pick a journal, or leave this on the template option and the format "
         "is taken from the file you uploaded.")

profile = None
if journal != "— use my uploaded template —":
    profile = next(p for p in all_profiles if p["name"] == journal)
elif tpl_bytes or tex_extras:
    try:
        src_name = tpl_up.name
        profile, tspec = discover.profile_from_template(
            src_name, tpl_bytes or next(iter(tex_extras.values())))
        st.caption(f"Format read from **{src_name}** "
                   f"(gaps filled from *{profile['derived_from']}*).")
    except Exception as exc:
        st.error(f"Could not read that template: {exc}")

if not up:
    st.info("Upload a manuscript to begin.")
    st.stop()
if not profile:
    st.info("Choose a journal above, or upload the journal's template.")
    st.stop()

# --- parse ---------------------------------------------------------------- #
sig = (up.name, up.size)
if ss.get("parse_sig") != sig:
    workdir = tempfile.mkdtemp(prefix="jformat_")
    src = os.path.join(workdir, up.name)
    with open(src, "wb") as fh:
        fh.write(up.getbuffer())
    with st.spinner("Reading manuscript…"):
        try:
            ss.ms = ingest.ingest(src, image_dir=os.path.join(workdir, "figures"))
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")
            st.stop()
    ss.parse_sig, ss.src = sig, src
    ss.built, ss.obj_edits, ss.accepted = None, {}, set()

ms: Manuscript = ss.ms
ss.setdefault("auto_done", None)

m = st.columns(5)
m[0].metric("Sections", len(ms.sections))
m[1].metric("Figures", len(ms.figures))
m[2].metric("Tables", len(ms.tables))
m[3].metric("Equations", len(ms.equations))
m[4].metric("References", len(ms.references))

@st.cache_data(show_spinner=False)
def _analysis(sig, journal_id, n_sec, n_fig, n_tab, n_ref, title,
              abstract_sig, kw_sig):
    """Everything that scans the whole manuscript, computed once.

    Streamlit re-executes this script top to bottom on every interaction. Left
    uncached, the document analysis ran again on each keystroke — which is what
    made the app feel slow on a long paper.
    """
    return {
        "report": checks.run(ms, profile),
        "objects": edits.summarise(ms),
        "missing_x": xref.find_missing(ms),
        "wc": compact.word_count(ms),
        "filler": compact.long_windedness(ms),
    }


@st.cache_data(show_spinner=False)
def _dupes(sig, n_sec, title):
    """The duplicate scan is the one costly pass, so it waits until the Length
    panel is actually opened rather than running on every upload."""
    return compact.duplicates(ms)


# the key carries everything the analysis depends on, so editing the abstract
# or keywords above recomputes rather than serving a stale report
_a = _analysis(ss.parse_sig, profile["id"], len(ms.sections), len(ms.figures),
               len(ms.tables), len(ms.references), ms.title,
               (len(ms.abstract), ms.abstract[:120]), tuple(ms.keywords))
report = _a["report"]
fails = [msg for lvl, msg in report if lvl == "fail"]
if fails:
    st.warning(f"**{len(fails)} thing(s) need your attention** — "
               "open *Review and fix* below. You can still format now.")

# =========================================================================== #
# 2. Optional review — everything lives behind one expander
# =========================================================================== #
st.subheader("2. Quick review (optional)")
st.caption("Your equations are preserved from the source by default. Only change an object if it was detected incorrectly.")

with st.expander("Title, authors, abstract, keywords"):
    ms.title = st.text_input("Title", ms.title)
    nm = st.text_input("Authors (comma-separated)",
                       ", ".join(a.name for a in ms.authors))
    old = {a.name: a for a in ms.authors}
    ms.authors = [old.get(n.strip(), Author(name=n.strip()))
                  for n in nm.split(",") if n.strip()]
    aff = st.text_input("Affiliation",
                        ms.authors[0].affiliation if ms.authors else "")
    for a in ms.authors:
        a.affiliation = aff
    ab_cfg = profile.get("abstract", {})
    if ab_cfg.get("type") == "structured":
        st.caption("This journal requires a structured abstract.")
        if ms.abstract and not ms.structured_abstract:
            st.text_area("Original abstract (copy from here)", ms.abstract,
                         height=90, disabled=True)
        for secname in ab_cfg.get("sections", []):
            v = st.text_area(secname, ms.structured_abstract.get(secname, ""),
                             height=68, key=f"sa_{secname}")
            if v.strip():
                ms.structured_abstract[secname] = v.strip()
            else:
                ms.structured_abstract.pop(secname, None)
    else:
        ms.abstract = st.text_area("Abstract", ms.abstract, height=150)
    ms.keywords = [k.strip() for k in
                   st.text_input("Keywords", ", ".join(ms.keywords)).split(",")
                   if k.strip()]

obj_rows = _a["objects"]
if obj_rows:
    with st.expander(f"Figures, tables and equations ({len(obj_rows)})"):
        st.caption("Change anything read wrongly. A Word file can't distinguish "
                   "an equation pasted as a picture from a figure.")
        for row in obj_rows:
            c = st.columns([1.3, 1.1, 2.2, 0.7])
            kinds = ["figure", "table", "equation"]
            k = c[0].selectbox(f"#{row['order']}", kinds,
                               index=kinds.index(row["kind"]),
                               key=f"k_{row['label']}")
            c[1].caption(row["detail"][:28])
            cap = c[2].text_input("Caption", row["caption"],
                                  key=f"c_{row['label']}",
                                  label_visibility="collapsed")
            drop = c[3].checkbox("Drop", key=f"d_{row['label']}")
            if drop:
                ss.obj_edits[row["label"]] = {"kind": "drop"}
            elif k != row["kind"] or cap != row["caption"]:
                ss.obj_edits[row["label"]] = {"kind": k, "caption": cap}
            else:
                ss.obj_edits.pop(row["label"], None)

missing_x = _a["missing_x"]
ss.setdefault("xref_ok", set())
if missing_x:
    with st.expander(f"Missing in-text mentions ({len(missing_x)})"):
        st.caption("Journals require every figure and table to be referred to in "
                   "the text. Each sentence below is built from the object's own "
                   "caption and added to the end of the preceding paragraph. "
                   "It states that the object exists — it makes no claim about "
                   "what the data shows, because that is yours to write.")
        if st.checkbox("Select all", key="xr_all"):
            ss.xref_ok = {x["label"] for x in missing_x}
        for x in missing_x:
            on = st.checkbox(f"**{x['kind'].title()} {x['number']}** — "
                             f"{x['sentence']}",
                             value=x["label"] in ss.xref_ok,
                             key=f"x_{x['label']}")
            st.caption(f"in *{x['section'][:45]}* · after: …{x['after'][-60:]}")
            if on:
                ss.xref_ok.add(x["label"])
            else:
                ss.xref_ok.discard(x["label"])

wc, filler = _a["wc"], _a["filler"]
ss.setdefault("cut_dupes", False)
ss.setdefault("cut_filler", False)
with st.expander(f"Length — {wc['body']:,} words in body, "
                 f"{wc['abstract']} in abstract"):
    dupes = _dupes(ss.parse_sig, len(ms.sections), ms.title)
    st.caption("Only edits that provably lose nothing are offered here. "
               "Genuine shortening means deciding which of your sentences matters "
               "least, which is not something this tool should do to a paper "
               "going out under your name.")
    st.markdown("**Where the length is**")
    for row in wc["per_section"][:6]:
        st.write(f"{row['words']:>5} words — {row['title'][:60]}")
    if dupes:
        st.markdown(f"**{len(dupes)} repeated sentence(s)** — the later copy can go")
        for dd in dupes[:6]:
            st.caption(f"{dd['score']:.0%} match · in *{dd['repeat']['section'][:32]}* "
                       f"· “{dd['repeat']['text'][:110]}…”")
        ss.cut_dupes = st.checkbox("Remove the repeated copies", value=ss.cut_dupes)
    if filler:
        tot = sum(f["approx_words"] for f in filler)
        st.markdown(f"**{sum(f['count'] for f in filler)} filler phrase(s)** "
                    f"≈ {tot} words")
        for f_ in filler[:6]:
            st.caption(f"{f_['count']}× “{f_['pattern'][2:-2]}” → "
                       f"“{f_['replacement'] or '(delete)'}”")
        ss.cut_filler = st.checkbox("Apply these substitutions", value=ss.cut_filler)
    if not dupes and not filler:
        st.success("No safe cuts found — the text is already tight.")
    st.info("To cut pages without cutting words, tick **Compact layout** in "
            "Output options. That tightens float spacing, captions and heading "
            "gaps, and typically saves a page or two.")

# Proposals carry callables, which cache_data cannot pickle — and it is cheap
# (measured in milliseconds), so it is simply recomputed.
props = prop_mod.build(ms, profile)
if props or ms.comments:
    label = f"Suggested fixes ({len(props)})"
    if ms.comments:
        label += f" · comments in file ({len(ms.comments)})"
    with st.expander(label):
        for c in ms.comments:
            st.markdown(f"💬 **{c['author']}**: {c['text']}")
        if ms.comments and props:
            st.divider()
        icon = {"fail": "🔴", "warn": "🟠", "info": "🔵"}
        for p_ in props:
            ok = st.checkbox(f"{icon.get(p_.severity,'')} {p_.title}",
                             value=(p_.id in ss.accepted) or p_.default,
                             key=f"p_{p_.id}")
            st.caption(p_.detail)
            if ok:
                ss.accepted.add(p_.id)
            else:
                ss.accepted.discard(p_.id)

with st.expander("Full check report"):
    for lvl, msg in report:
        {"ok": st.success, "warn": st.warning, "fail": st.error}[lvl](msg)

with st.expander("Output options"):
    eq_mode = st.radio(
        "Equations", ["native", "image"], horizontal=True,
        format_func=lambda x: ("Preserve original Word equations (recommended)"
                               if x == "native" else "Render as images (compatibility mode)"),
        help="Preserve original keeps Word/OMML equations editable and avoids LaTeX-to-image conversion errors. Use image mode only when a viewer cannot display Word equations."
    )
    keep_masthead = True
    if tpl_bytes:
        inv = template_probe.inventory(tpl_bytes)
        keep_masthead = st.checkbox(
            f"Keep the template's logo and masthead ({inv['logos']} image(s))",
            value=True)
        if inv["masthead_lines"]:
            st.caption(" · ".join(inv["masthead_lines"][:2]))
    float_mode = st.radio(
        "LaTeX table & figure placement", ["journal", "inline"], horizontal=True,
        format_func=lambda x: ("Float like a real journal (recommended)"
                               if x == "journal"
                               else "Pin in place, matching the preview"),
        help="LaTeX normally moves tables and figures to the top or bottom of a "
             "page — that is how typeset papers look, and what reviewers expect, "
             "but the PDF will not match the Word preview page for page. Pin in "
             "place keeps every object exactly where it sits in your text, at the "
             "cost of more whitespace.")
    compact_layout = st.checkbox(
        "Compact layout (fewer pages, no words removed)", value=False,
        help="Tightens float spacing, caption size, display-maths gaps, list "
             "spacing and heading gaps in the LaTeX output. Nothing you wrote "
             "changes.")
    # LibreOffice takes ~8s per pass on a long paper, and it was running twice
    # on every Format (page images + PDF). Both are now produced on request.
    st.caption("Exact page images and PDF export are produced on demand — "
               "press Format for the instant preview first.")

# =========================================================================== #
# 3. Format
# =========================================================================== #
st.subheader("3. Download")


stamp = (profile["id"], eq_mode, float_mode, compact_layout, bool(tpl_bytes),
         keep_masthead, tuple(sorted(ss.xref_ok)), ss.cut_dupes, ss.cut_filler,
         tuple(sorted(tex_extras)), tuple(sorted(ss.accepted)),
         tuple(sorted((k, str(v)) for k, v in ss.obj_edits.items())),
         ms.title, ms.abstract[:200], tuple(ms.keywords), len(ms.sections))

if st.button("⚡ Format now", type="primary", use_container_width=True):
    ss.auto_done = ss.parse_sig
    with st.spinner("Formatting…"):
        out = edits.apply(copy.deepcopy(ms), ss.obj_edits)
        out, applied = prop_mod.apply_selected(
            out, prop_mod.build(out, profile), ss.accepted)
        if ss.xref_ok:
            added = xref.apply(out, xref.find_missing(out), ss.xref_ok)
            if added:
                applied.append(f"added in-text mentions for {len(added)} object(s)")
        if ss.cut_dupes:
            n = compact.remove_duplicates(out, compact.duplicates(out))
            if n:
                applied.append(f"removed {n} duplicated words")
        if ss.cut_filler:
            n = compact.apply_long_windedness(
                out, compact.long_windedness(out))
            if n:
                applied.append(f"tightened {n} filler words")
        tpl_warning = ""
        try:
            docx_bytes = render_docx.render(out, profile, equation_mode=eq_mode,
                                            template_bytes=tpl_bytes,
                                            keep_masthead=keep_masthead)
        except Exception as exc:
            if not tpl_bytes:
                raise
            # A template can be malformed in ways no amount of guarding
            # anticipates. Losing the whole run over it is the wrong outcome:
            # fall back to building from the profile and say so plainly.
            tpl_warning = (f"Could not build inside the template ({type(exc).__name__}: "
                           f"{exc}). The Word file was built from the "
                           f"**{profile['name']}** profile instead, so its "
                           "layout is right but the template's logo and styles "
                           "are not included. The LaTeX output is unaffected.")
            docx_bytes = render_docx.render(out, profile, equation_mode=eq_mode)
        ss.built = {
            "docx": docx_bytes,
            "zip": render_latex.build_zip(out, profile, extra_files=tex_extras,
                                          float_mode=float_mode,
                                          compact=compact_layout),
            "tex": render_latex.render(out, profile, float_mode=float_mode,
                                       compact=compact_layout),
            "html": preview.html_preview(out, profile),
            "pages": None,      # rendered on request; see below
            "pdf": None,
            "has_class": render_latex._class_available(profile, tex_extras),
            "applied": applied,
            "tpl_warning": tpl_warning,
        }
        ss.stamp = stamp
        svc_mod.log_run(profile["name"], len(out.figures), len(out.tables),
                        compact.word_count(out)["body"])

if ss.built is None:
    st.info("Ready. Press **⚡ Format now** once to build the submission files.")
    st.stop()

if ss.stamp != stamp:
    st.warning("Something changed — press *Format now* again to refresh.")

b = ss.built
if b.get("tpl_warning"):
    st.warning(b["tpl_warning"])
if b["applied"]:
    st.success("Applied: " + "; ".join(b["applied"]))

# --- downloads first: it is what the user came for ------------------------ #
stem = Path(up.name).stem
d = st.columns(3)
d[0].download_button("⬇ Word (.docx)", b["docx"],
                     file_name=f"{stem}_{profile['id']}.docx",
                     use_container_width=True,
                     mime="application/vnd.openxmlformats-officedocument."
                          "wordprocessingml.document")
d[1].download_button("⬇ LaTeX (.zip)", b["zip"],
                     file_name=f"{stem}_{profile['id']}_latex.zip",
                     mime="application/zip", use_container_width=True)
with d[2]:
    if b.get("pdf"):
        st.download_button("⬇ PDF", b["pdf"], file_name=f"{stem}.pdf",
                           mime="application/pdf", use_container_width=True)
    elif preview.soffice_available():
        if st.button("Make PDF", use_container_width=True,
                     help="Converts the Word file — takes a few seconds."):
            with st.spinner("Converting…"):
                b["pdf"] = preview.render_pdf(b["docx"])
            st.rerun()
    else:
        st.caption("PDF needs LibreOffice installed.")

if b["has_class"]:
    st.caption("LaTeX: upload the zip to Overleaf, set **main.tex** as the main "
               "document, Recompile. The class is included or already on Overleaf.")
else:
    st.caption(f"LaTeX: `{profile['latex'].get('class_file')}` isn't a standard "
               "Overleaf class, so main.tex uses an `article` preamble matching "
               "this journal's geometry. It compiles anywhere; upload the "
               "journal's LaTeX kit above for the exact style.")

# --- preview -------------------------------------------------------------- #
t1, t2 = st.tabs(["Preview", "main.tex"])
with t1:
    if b.get("pages"):
        for i, png in enumerate(b["pages"], 1):
            st.image(png, caption=f"Page {i}", use_container_width=True)
        if b.get("more_pages"):
            if st.button("Render the rest"):
                with st.spinner("Rendering…"):
                    b["pages"] = preview.render_pages(b["docx"], max_pages=40)
                    b["more_pages"] = False
                st.rerun()
    else:
        if preview.soffice_available():
            pc1, pc2 = st.columns([1, 2])
            if pc1.button("Show exact pages", use_container_width=True):
                with st.spinner("Rendering the first pages…"):
                    b["pages"] = preview.render_pages(b["docx"], max_pages=3)
                    b["more_pages"] = True
                st.rerun()
            pc2.caption("Renders the real .docx through LibreOffice — exact "
                        "pagination, a few seconds.")
        st.components.v1.html(
            f'<div style="background:#e9e9ee;padding:20px 6px">{b["html"]}</div>',
            height=850, scrolling=True)
with t2:
    st.code(b["tex"][:20000], language="latex")

# --- offer help with what the tool found ---------------------------------- #
def _relevant(report, ms):
    """Map what the checker found onto services worth offering.

    An offer tied to a real, detected problem reads as helpful. A generic
    banner reads as an advert, so a clean manuscript gets nothing.
    """
    problems = " ".join(m.lower() for lvl, m in report if lvl in ("fail", "warn"))
    keys, why = [], []
    if "no image" in problems or "artwork" in problems:
        keys.append("figures"); why.append("figures missing their artwork")
    if "past the reference list" in problems:
        keys.append("manuscript"); why.append("citations with no reference entry")
    if "abstract is" in problems and "limit" in problems:
        keys.append("manuscript"); why.append("an abstract over the limit")
    if "structured abstract" in problems:
        keys.append("manuscript"); why.append("a structured abstract to write")
    if getattr(ms, "comments", None):
        keys.append("response"); why.append("reviewer comments still open")
    if len(getattr(ms, "tables", [])) >= 8:
        keys.append("stats"); why.append("a lot of results tables")
    if getattr(ms, "equations", None):
        keys.append("code")
    return list(dict.fromkeys(keys)), why


svc_keys, svc_why = _relevant(report, ms)
if svc_keys:
    st.markdown("---")
    with st.container(border=True):
        if svc_why:
            st.markdown(f"**This manuscript has {', '.join(svc_why[:2])}.** "
                        "If you would rather hand that over:")
        cols = st.columns(min(len(svc_keys), 3))
        for col, k in zip(cols, svc_keys[:3]):
            sv = svc_mod.BY_ID[k]
            with col:
                st.markdown(f"### {sv['icon']}")
                st.markdown(f"**{sv['title']}**")
                st.caption(sv["blurb"])
                st.page_link("pages/1_Services.py", label="Request this →")

with st.expander("Get a free 15-minute review of this manuscript"):
    st.caption("Optional — your files are already downloaded above. "
               "Tell us where you're stuck and we'll come back with specifics.")
    q1, q2 = st.columns(2)
    q_name = q1.text_input("Name *", client.prefill("name"), key="q_name")
    q_email = q2.text_input("Email *", client.prefill("email"), key="q_email")
    q3, q4 = st.columns(2)
    q_phone = q3.text_input("Phone / WhatsApp", client.prefill("phone"),
                            key="q_phone")
    q_inst = q4.text_input("University / Institution",
                           client.prefill("institution"), key="q_inst")
    q_service = st.selectbox(
        "What would help most?",
        [svc_mod.BY_ID[k]["title"] for k in
         (svc_keys or ["manuscript"])] +
        [v["title"] for k, v in svc_mod.BY_ID.items() if k not in svc_keys],
        key="q_service")
    q_msg = st.text_area("Anything else?", key="q_msg", height=90,
                         placeholder="Target journal, deadline, where you're stuck…")
    if st.button("Send", key="q_send", type="primary"):
        sid = next((k for k, v in svc_mod.BY_ID.items()
                    if v["title"] == q_service), "manuscript")
        ok, msg = svc_mod.submit(sid, {
            "name": q_name, "email": q_email, "phone": q_phone,
            "institution": q_inst, "journal": profile["name"],
            "message": q_msg}, source="formatter tool")
        (st.success if ok else st.error)(msg)

theme.footer()
