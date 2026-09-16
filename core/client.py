"""Per-visitor personalisation.

A researcher who tells us once that they work on SCADA intrusion detection and
are targeting IEEE Access should not be asked again on the next page. That
context is used to prefill forms, pre-select their usual journal, seed the
Journal Finder, and address them by name.

Deliberate design choices:

* **Optional.** Every tool works fully without a profile. Nothing is gated on
  giving us a name.
* **Chosen, not observed.** The profile holds only what someone typed into it.
  There is no fingerprinting and no cookie tracking; researchers upload
  unpublished manuscripts here, and quiet observation would be a betrayal of
  that even where it would be legal.
* **Portable by code.** A short code lets someone return on another device, or
  lets you set up a profile for a client you already work with and send it to
  them. That is what makes this feel personal for existing clients rather than
  anonymous.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import streamlit as st

DB_PATH = os.environ.get("JF_DB", "data/leads.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    code        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    last_seen   TEXT,
    name        TEXT,
    email       TEXT,
    phone       TEXT,
    institution TEXT,
    country     TEXT,
    role        TEXT,
    domain      TEXT,
    journals    TEXT,
    notes       TEXT,
    managed     INTEGER NOT NULL DEFAULT 0
);
"""

FIELDS = ("name", "email", "phone", "institution", "country", "role",
          "domain", "journals", "notes")


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_code() -> str:
    """Short, readable, no ambiguous characters — people retype these."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "CS-" + "".join(secrets.choice(alphabet) for _ in range(6))


def save(profile: dict, code: str | None = None, managed: bool = False) -> str:
    code = code or profile.get("code") or new_code()
    vals = {f: (profile.get(f) or "")[:400] for f in FIELDS}
    with _db() as conn:
        exists = conn.execute("SELECT 1 FROM clients WHERE code=?",
                              (code,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE clients SET last_seen=?, name=?, email=?, phone=?, "
                "institution=?, country=?, role=?, domain=?, journals=?, "
                "notes=? WHERE code=?",
                (_now(), *[vals[f] for f in FIELDS], code))
        else:
            conn.execute(
                "INSERT INTO clients (code, created_at, last_seen, name, email,"
                " phone, institution, country, role, domain, journals, notes,"
                " managed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, _now(), _now(), *[vals[f] for f in FIELDS],
                 1 if managed else 0))
    return code


def load(code: str) -> dict | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM clients WHERE code=?",
                           (code,)).fetchone()
        if row:
            conn.execute("UPDATE clients SET last_seen=? WHERE code=?",
                         (_now(), code))
    return dict(row) if row else None


def all_clients() -> list[dict]:
    with _db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM clients ORDER BY last_seen DESC").fetchall()]


def delete(code: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM clients WHERE code=?", (code,))


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
def current() -> dict:
    """The active profile for this browser session ({} when none)."""
    init()
    ss = st.session_state
    if ss.get("client"):
        return ss["client"]
    # a code can arrive in the URL, which is how you hand one to a client
    try:
        code = st.query_params.get("c", "")
    except Exception:
        code = ""
    if code:
        prof = load(code)
        if prof:
            ss["client"] = prof
            return prof
    return {}


def greeting() -> str:
    p = current()
    name = (p.get("name") or "").strip()
    if not name:
        return ""
    # "Dr. Meera Nair" should greet Meera, not "Dr."
    TITLES = {"dr", "dr.", "prof", "prof.", "mr", "mr.", "mrs", "mrs.",
              "ms", "ms.", "miss", "er", "er.", "shri", "smt"}
    parts = [w for w in name.split() if w.lower().strip(".") not in
             {t.strip(".") for t in TITLES}]
    first = parts[0] if parts else name.split()[0]
    return f"Welcome back, {first}."


def prefill(field: str, default: str = "") -> str:
    return (current().get(field) or default)


def journals() -> list[str]:
    raw = current().get("journals") or ""
    return [j.strip() for j in raw.split(",") if j.strip()]


def panel(compact: bool = True) -> None:
    """The profile control. Shown in the sidebar on every page."""
    init()
    ss = st.session_state
    prof = current()

    with st.sidebar:
        st.markdown("---")
        if prof:
            st.markdown(f"**{prof.get('name') or 'Your profile'}**")
            bits = [prof.get("institution"), prof.get("domain")]
            sub = " · ".join(b for b in bits if b)
            if sub:
                st.caption(sub)
            st.caption(f"Code `{prof['code']}` — keep this to return on any "
                       "device.")
            with st.expander("Edit my details"):
                _form(prof)
            if st.button("Sign out of profile", use_container_width=True):
                ss.pop("client", None)
                try:
                    st.query_params.clear()
                except Exception:
                    pass
                st.rerun()
        else:
            with st.expander("Personalise this tool", expanded=False):
                st.caption("Optional. Tell us once and every form here is "
                           "prefilled, and your usual journals come up first.")
                code = st.text_input("Have a code?", key="cl_code",
                                     placeholder="CS-XXXXXX")
                if st.button("Load my profile", use_container_width=True):
                    p = load(code)
                    if p:
                        ss["client"] = p
                        st.rerun()
                    else:
                        st.error("No profile with that code.")
                st.markdown("**or set one up**")
                _form({})


def _form(prof: dict) -> None:
    ss = st.session_state
    name = st.text_input("Name", prof.get("name", ""), key="cl_name")
    email = st.text_input("Email", prof.get("email", ""), key="cl_email")
    inst = st.text_input("Institution", prof.get("institution", ""),
                         key="cl_inst")
    domain = st.text_input("Research area", prof.get("domain", ""),
                           key="cl_domain",
                           placeholder="e.g. intrusion detection")
    journals_ = st.text_input("Usual target journals", prof.get("journals", ""),
                              key="cl_journals",
                              placeholder="IEEE Access, IJIES")
    if st.button("Save profile", use_container_width=True, type="primary",
                 key="cl_save"):
        if len((name or "").strip()) < 2:
            st.error("Please enter your name.")
            return
        merged = dict(prof)
        merged.update({"name": name, "email": email, "institution": inst,
                       "domain": domain, "journals": journals_})
        code = save(merged, prof.get("code"))
        merged["code"] = code
        ss["client"] = merged
        st.success(f"Saved. Your code is {code}")
        st.rerun()
