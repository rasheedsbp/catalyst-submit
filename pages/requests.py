"""Password-protected, requests-only admin console.

This page is intentionally omitted from the public sidebar. Open it directly at
``/requests`` on the deployed Streamlit app. The admin can see and manage only
rows from the ``requests`` table; client profiles and anonymous usage/runs are
not exposed here.
"""
from __future__ import annotations

import hmac
import os
from datetime import date, datetime

import streamlit as st

from core import services as svc_mod, theme

st.set_page_config(page_title="Requests Admin", page_icon="🔒", layout="wide")
svc_mod.init()
theme.sidebar()

ADMIN_KEY = os.environ.get("JF_ADMIN_KEY", "")
PUBLIC_URL = os.environ.get("JF_PUBLIC_URL", "").rstrip("/")


def _login() -> None:
    """Render password gate and stop until authenticated."""
    if not ADMIN_KEY:
        theme.header("Requests Admin", "Disabled")
        st.error("Admin access is disabled. Set JF_ADMIN_KEY on the server.")
        st.stop()

    if st.session_state.get("admin_ok") is True:
        return

    theme.header("Requests Admin", "Restricted access")
    st.caption("Only authorised administrators can view service requests.")
    with st.form("admin_login", clear_on_submit=True):
        entered = st.text_input("Admin password", type="password",
                                autocomplete="current-password")
        submitted = st.form_submit_button("Open requests", type="primary",
                                          use_container_width=True)
    if submitted:
        if entered and hmac.compare_digest(entered, ADMIN_KEY):
            st.session_state.admin_ok = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        # Stored timestamps are ISO-8601; date part is sufficient for filtering.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _service_title(service_id: str) -> str:
    return svc_mod.BY_ID.get(service_id, {}).get("title", service_id or "—")


def _filter_rows(rows: list[dict], *, status: str, query: str,
                 start: date | None, end: date | None) -> list[dict]:
    q = (query or "").strip().lower()
    out: list[dict] = []
    for r in rows:
        if status != "All" and r.get("status") != status:
            continue
        rd = _as_date(r.get("at"))
        if start and (rd is None or rd < start):
            continue
        if end and (rd is None or rd > end):
            continue
        if q:
            searchable = " ".join(str(r.get(k) or "") for k in (
                "id", "name", "email", "phone", "institution", "country",
                "role", "domain", "journal", "deadline", "message",
                "source", "status", "notes", "service",
            )).lower()
            if q not in searchable and q not in _service_title(r.get("service", "")).lower():
                continue
        out.append(r)
    return out


_login()

theme.header("Requests Admin", "Requests only")

# Header actions. The direct URL is deliberately not placed in public navigation.
h1, h2, h3 = st.columns([3, 1, 1])
with h1:
    st.caption("Private request inbox · exact dates · status management")
    if PUBLIC_URL:
        st.caption(f"Direct admin link: {PUBLIC_URL}/requests")
with h2:
    if st.button("↻ Refresh", use_container_width=True):
        st.rerun()
with h3:
    if st.button("Log out", use_container_width=True):
        st.session_state.pop("admin_ok", None)
        st.rerun()

all_rows = svc_mod.all_requests()

# --------------------------------------------------------------------------- #
# Filters
st.subheader("Requests")
f1, f2, f3, f4 = st.columns([1.1, 2.2, 1.2, 1.2])
status = f1.selectbox("Status", ["All"] + svc_mod.STATUSES)
query = f2.text_input("Search", placeholder="Name, email, journal, institution…")
use_dates = st.toggle("Filter by date", value=False)
start_date: date | None = None
end_date: date | None = None
if use_dates:
    today = date.today()
    start_date = f3.date_input("From", value=today)
    end_date = f4.date_input("To", value=today)
    if start_date > end_date:
        st.error("The From date must be before or equal to the To date.")
        st.stop()
else:
    f3.caption("Date filter off")
    f4.caption("Showing all dates")

rows = _filter_rows(all_rows, status=status, query=query,
                    start=start_date, end=end_date)

m1, m2, m3 = st.columns(3)
m1.metric("Visible requests", len(rows))
m2.metric("New", sum(1 for r in rows if r.get("status") == "New"))
m3.metric("All requests", len(all_rows))

if rows:
    st.download_button(
        "⬇ Export requests as CSV",
        svc_mod.to_csv_rows(rows),
        file_name="service_requests.csv",
        mime="text/csv",
    )

    # Compact DB-viewer-like table first; expand a row below for full detail.
    table_rows = []
    for r in rows:
        table_rows.append({
            "ID": r.get("id"),
            "Date / time": r.get("at") or "",
            "Status": r.get("status") or "",
            "Service": _service_title(r.get("service", "")),
            "Name": r.get("name") or "",
            "Email": r.get("email") or "",
            "Phone": r.get("phone") or "",
            "Institution": r.get("institution") or "",
            "Journal": r.get("journal") or "",
            "Deadline": r.get("deadline") or "",
        })
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Request details")
    for r in rows:
        title = _service_title(r.get("service", ""))
        when = (r.get("at") or "").replace("T", " ")
        head = f"#{r['id']} · {r.get('status', 'New')} · {r.get('name', '—')} · {title} · {when}"
        with st.expander(head, expanded=False):
            a, b = st.columns(2)
            with a:
                st.markdown(f"**Date/time:** {r.get('at') or '—'}")
                st.markdown(f"**Name:** {r.get('name') or '—'}")
                st.markdown(f"**Email:** {r.get('email') or '—'}")
                st.markdown(f"**Phone:** {r.get('phone') or '—'}")
                st.markdown(f"**Institution:** {r.get('institution') or '—'}")
                st.markdown(f"**Country:** {r.get('country') or '—'}")
            with b:
                st.markdown(f"**Service:** {title}")
                st.markdown(f"**Role:** {r.get('role') or '—'}")
                st.markdown(f"**Research area:** {r.get('domain') or '—'}")
                st.markdown(f"**Target journal:** {r.get('journal') or '—'}")
                st.markdown(f"**Timeline:** {r.get('deadline') or '—'}")
                st.markdown(f"**Source:** {r.get('source') or '—'}")

            st.markdown("**Message**")
            st.write(r.get("message") or "—")

            st.markdown("---")
            u1, u2 = st.columns([1, 3])
            current = r.get("status") if r.get("status") in svc_mod.STATUSES else "New"
            new_status = u1.selectbox(
                "Status",
                svc_mod.STATUSES,
                index=svc_mod.STATUSES.index(current),
                key=f"st_{r['id']}",
            )
            notes = u2.text_input(
                "Internal notes",
                r.get("notes") or "",
                key=f"nt_{r['id']}",
            )
            if st.button("Save request", key=f"sv_{r['id']}", type="primary"):
                svc_mod.update_request(r["id"], new_status, notes)
                st.success("Request updated.")
                st.rerun()
else:
    st.info("No requests match these filters.")

theme.footer()
