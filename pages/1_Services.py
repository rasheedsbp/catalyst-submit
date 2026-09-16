"""Services — browse, click one, send the details."""
from __future__ import annotations

import streamlit as st

from core import services as svc_mod, theme, nav, client

st.set_page_config(page_title="Services · CS Software Solutions",
                   page_icon="🧩", layout="wide")
svc_mod.init()
theme.header("Our Services", "Research support for researchers worldwide")
nav.bar("pages/1_Services.py")
theme.sidebar()
client.panel()

_g = client.greeting()
if _g:
    st.caption(_g + " Your details below are already filled in.")

ss = st.session_state
ss.setdefault("chosen", None)

COUNTRIES = ["India", "United States", "United Kingdom", "Nigeria", "Malaysia",
             "Saudi Arabia", "United Arab Emirates", "Egypt", "Indonesia",
             "Pakistan", "Bangladesh", "Ethiopia", "South Africa", "Other"]
ROLES = ["PhD scholar", "Postgraduate student", "Faculty / Professor",
         "Postdoctoral researcher", "Independent researcher", "Other"]
DEADLINES = ["Within a week", "2–4 weeks", "1–3 months", "No fixed deadline"]

# --------------------------------------------------------------------------- #
# 1. pick a service
# --------------------------------------------------------------------------- #
if ss.chosen is None:
    theme.hero(
        "What do you need help with?",
        "Choose a service and tell us about your work. We reply within one "
        "working day with a realistic assessment — not a sales pitch.")

    cols = st.columns(2)
    for i, s in enumerate(svc_mod.CATALOG):
        with cols[i % 2]:
            theme.service_card(s)
            if st.button(f"Request {s['title']}", key=f"pick_{s['id']}",
                         use_container_width=True, type="primary"):
                ss.chosen = s["id"]
                st.rerun()
            st.write("")
    theme.footer()
    st.stop()

# --------------------------------------------------------------------------- #
# 2. the request form
# --------------------------------------------------------------------------- #
s = svc_mod.BY_ID[ss.chosen]

if st.button("← All services"):
    ss.chosen = None
    st.rerun()

theme.hero(f"{s['icon']}  {s['title']}", s["blurb"])

left, right = st.columns([2, 1])

with right:
    st.markdown("**What this covers**")
    for d in s["details"]:
        st.markdown(f"- {d}")
    st.markdown("---")
    st.caption("Your details go only to our team. We don't share them, and we "
               "don't add you to a mailing list.")

with left:
    st.markdown("**Your details**")
    c1, c2 = st.columns(2)
    name = c1.text_input("Full name *", client.prefill("name"),
                     key="f_name")
    email = c2.text_input("Email *", client.prefill("email"),
                      key="f_email")

    c3, c4 = st.columns(2)
    phone = c3.text_input("Phone / WhatsApp", client.prefill("phone"),
                          key="f_phone", placeholder="+91…")
    _c = client.prefill("country")
    country = c4.selectbox("Country", COUNTRIES, key="f_country",
                           index=COUNTRIES.index(_c) if _c in COUNTRIES else 0)

    c5, c6 = st.columns(2)
    institution = c5.text_input("University / Institution",
                                client.prefill("institution"), key="f_inst")
    _r = client.prefill("role")
    role = c6.selectbox("You are a", ROLES, key="f_role",
                        index=ROLES.index(_r) if _r in ROLES else 0)

    st.markdown("**About the work**")
    c7, c8 = st.columns(2)
    domain = c7.text_input("Research area", client.prefill("domain"),
                           key="f_domain",
                           placeholder="e.g. intrusion detection, SCADA security")
    journal = c8.text_input("Target journal (if known)",
                            ", ".join(client.journals()), key="f_journal",
                            placeholder="e.g. IEEE Access, IJIES")
    deadline = st.radio("Timeline", DEADLINES, horizontal=True, key="f_deadline")
    message = st.text_area(
        "Tell us what you need *", key="f_msg", height=130,
        placeholder="What stage is the work at, what's already done, and where "
                    "are you stuck? The more specific you are, the more useful "
                    "our first reply will be.")

    agree = st.checkbox("I'm happy to be contacted about this request",
                        value=True, key="f_agree")

    if st.button("Send request", type="primary", use_container_width=True,
                 disabled=not agree):
        ok, msg = svc_mod.submit(s["id"], {
            "name": name, "email": email, "phone": phone,
            "institution": institution, "country": country, "role": role,
            "domain": domain, "journal": journal, "deadline": deadline,
            "message": message,
        }, source="services page")
        if ok:
            client.save({"name": name, "email": email, "phone": phone,
                         "institution": institution, "country": country,
                         "role": role, "domain": domain, "journals": journal},
                        (client.current() or {}).get("code"))
            st.success(msg)
            st.balloons()
            ss.chosen = None
        else:
            st.error(msg)

theme.footer()
