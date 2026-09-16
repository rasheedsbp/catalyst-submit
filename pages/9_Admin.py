"""Admin — every request, its status, and how the tool is being used."""
from __future__ import annotations

import os

import streamlit as st

from core import env as _env  # noqa: F401
from core import services as svc_mod, theme, client

st.set_page_config(page_title="Admin", page_icon="🔒", layout="wide")
svc_mod.init()
theme.sidebar()

KEY = os.environ.get("JF_ADMIN_KEY", "")
if not KEY:
    st.error("Set JF_ADMIN_KEY in the environment to enable this page.")
    st.stop()

if st.session_state.get("admin_ok") is not True:
    theme.header("Admin", "Restricted")
    entered = st.text_input("Access key", type="password")
    if st.button("Open", type="primary"):
        if entered and entered == KEY:
            st.session_state.admin_ok = True
            st.rerun()
        else:
            st.error("Incorrect key.")
    st.stop()

theme.header("Admin", "Requests and usage")

s = svc_mod.stats()
m = st.columns(5)
m[0].metric("Requests", s["requests"])
m[1].metric("New / unactioned", s["new"])
m[2].metric("Won", s["won"])
m[3].metric("Documents formatted", s["runs"])
m[4].metric("Tool → request", f"{(s['requests'] / s['runs'] * 100):.1f}%"
            if s["runs"] else "—")

tab_req, tab_daily, tab_clients, tab_insight = st.tabs(
    ["Requests", "Daily activity", "Client profiles", "Usage insight"])

# --------------------------------------------------------------------------- #
with tab_req:
    c1, c2 = st.columns([1, 3])
    flt = c1.selectbox("Status", ["All"] + svc_mod.STATUSES)
    rows = svc_mod.all_requests(flt)
    c2.caption(f"{len(rows)} request(s)")

    if not rows:
        st.info("No requests yet.")
    else:
        st.download_button("⬇ Export all as CSV", svc_mod.to_csv(),
                           file_name="service_requests.csv", mime="text/csv")
        for r in rows:
            title = svc_mod.BY_ID.get(r["service"], {}).get("title", r["service"])
            head = (f"{r['status']} · {title} · {r['name']} "
                    f"({r['institution'] or r['country'] or '—'}) · "
                    f"{(r['at'] or '')[:10]}")
            with st.expander(head, expanded=(r["status"] == "New")):
                a, b = st.columns(2)
                with a:
                    st.markdown(f"**Email:** {r['email']}")
                    st.markdown(f"**Phone:** {r['phone'] or '—'}")
                    st.markdown(f"**Institution:** {r['institution'] or '—'}")
                    st.markdown(f"**Country:** {r['country'] or '—'}")
                    st.markdown(f"**Role:** {r['role'] or '—'}")
                with b:
                    st.markdown(f"**Research area:** {r['domain'] or '—'}")
                    st.markdown(f"**Target journal:** {r['journal'] or '—'}")
                    st.markdown(f"**Timeline:** {r['deadline'] or '—'}")
                    st.markdown(f"**Came from:** {r['source'] or '—'}")
                st.markdown("**Their message**")
                st.write(r["message"] or "—")

                st.markdown("---")
                u1, u2 = st.columns([1, 3])
                new_status = u1.selectbox(
                    "Status", svc_mod.STATUSES,
                    index=svc_mod.STATUSES.index(r["status"])
                    if r["status"] in svc_mod.STATUSES else 0,
                    key=f"st_{r['id']}")
                notes = u2.text_input("Internal notes", r["notes"] or "",
                                      key=f"nt_{r['id']}")
                if st.button("Save", key=f"sv_{r['id']}"):
                    svc_mod.update_request(r["id"], new_status, notes)
                    st.success("Updated.")
                    st.rerun()

# --------------------------------------------------------------------------- #
with tab_daily:
    from core import notify

    d1, d2 = st.columns([1, 2])
    span = d1.selectbox("Period", [7, 14, 30, 90], index=2,
                        format_func=lambda n: f"Last {n} days")
    activity = svc_mod.daily_activity(span)

    if not activity:
        st.info("No activity yet.")
    else:
        st.dataframe(activity, use_container_width=True, hide_index=True,
                     column_config={
                         "date": "Date",
                         "requests": st.column_config.NumberColumn("Requests"),
                         "formatted": st.column_config.NumberColumn(
                             "Documents formatted")})
        st.caption("Pick a date to see who wrote in that day.")
        chosen = st.selectbox("Date", [a["date"] for a in activity])
        for r in svc_mod.requests_on(chosen):
            title = svc_mod.BY_ID.get(r["service"], {}).get("title", r["service"])
            st.markdown(
                f"**{(r['at'] or '')[11:16]} UTC · {title} · {r['status']}** — "
                f"{r['name']} <{r['email']}>"
                f"{' · ' + r['institution'] if r['institution'] else ''}")
            if r["message"]:
                st.caption(r["message"][:300])

    st.markdown("---")
    st.subheader("Email notifications")
    if notify.configured():
        st.success(f"Configured — alerts go to {', '.join(notify.TO)}")
        e1, e2 = st.columns(2)
        if e1.button("Send a test email"):
            ok, msg = notify.test()
            (st.success if ok else st.error)(msg)
        if e2.button("Send yesterday's digest now"):
            ok, msg = notify.digest(1)
            (st.success if ok else st.error)(msg)
        st.caption("For an automatic daily digest, schedule `digest.py` — see "
                   "SETUP.md.")
    else:
        st.info("Not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and "
                "NOTIFY_TO in your .env to get an email the moment a request "
                "arrives, plus a daily summary.")

# --------------------------------------------------------------------------- #
with tab_clients:
    client.init()
    st.caption("Create a profile for a client you already work with and send "
               "them the link — the tools then open prefilled, with their "
               "usual journals first. This is what makes it feel personal "
               "rather than anonymous.")

    with st.expander("Create a profile for a client", expanded=False):
        n1, n2 = st.columns(2)
        c_name = n1.text_input("Name", key="nc_name")
        c_email = n2.text_input("Email", key="nc_email")
        n3, n4 = st.columns(2)
        c_inst = n3.text_input("Institution", key="nc_inst")
        c_phone = n4.text_input("Phone", key="nc_phone")
        c_domain = st.text_input("Research area", key="nc_domain")
        c_journals = st.text_input("Usual target journals", key="nc_journals",
                                   placeholder="IEEE Access, IJIES")
        c_notes = st.text_area("Internal notes", key="nc_notes", height=70)
        if st.button("Create profile", type="primary"):
            if len((c_name or "").strip()) < 2:
                st.error("Enter a name.")
            else:
                code = client.save(
                    {"name": c_name, "email": c_email, "institution": c_inst,
                     "phone": c_phone, "domain": c_domain,
                     "journals": c_journals, "notes": c_notes},
                    managed=True)
                st.success(f"Created. Code: **{code}**")
                base = os.environ.get("JF_PUBLIC_URL", "")
                if base:
                    st.code(f"{base}/?c={code}", language=None)
                    st.caption("Send them that link — it loads their profile "
                               "automatically.")
                st.rerun()

    rows = client.all_clients()
    if not rows:
        st.info("No client profiles yet.")
    else:
        st.write(f"{len(rows)} profile(s)")
        for r in rows:
            tag = " · set up by us" if r["managed"] else ""
            with st.expander(f"{r['code']} · {r['name'] or 'unnamed'}"
                             f" · {r['institution'] or '—'}{tag}"):
                a, b = st.columns(2)
                a.markdown(f"**Email:** {r['email'] or '—'}")
                a.markdown(f"**Phone:** {r['phone'] or '—'}")
                a.markdown(f"**Country:** {r['country'] or '—'}")
                b.markdown(f"**Research area:** {r['domain'] or '—'}")
                b.markdown(f"**Journals:** {r['journals'] or '—'}")
                b.markdown(f"**Last seen:** {(r['last_seen'] or '')[:10]}")
                if r["notes"]:
                    st.caption(r["notes"])
                base = os.environ.get("JF_PUBLIC_URL", "")
                if base:
                    st.code(f"{base}/?c={r['code']}", language=None)
                if st.button("Delete", key=f"del_{r['code']}"):
                    client.delete(r["code"])
                    st.rerun()

# --------------------------------------------------------------------------- #
with tab_insight:
    st.subheader("Which services people ask for")
    if s["by_service"]:
        for name, n in s["by_service"]:
            st.write(f"{n:>4}  {name}")
    else:
        st.caption("No requests yet.")

    st.subheader("Most-formatted journals")
    st.caption("Worth watching: this tells you which journal profiles to add "
               "next, and which venues your prospective clients target.")
    if s["by_journal"]:
        for name, n in s["by_journal"]:
            st.write(f"{n:>4}  {name}")
    else:
        st.caption("No documents formatted yet.")

theme.footer()
