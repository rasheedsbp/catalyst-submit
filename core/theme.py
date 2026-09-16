"""Visual identity, taken from the CS Software Solutions logo.

Colours are sampled from the logo itself rather than guessed, so the tool sits
alongside the main site instead of looking like a separate product.
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

import streamlit as st

NAVY = "#08238A"
SKY = "#50B4EF"
PINK = "#EA2D7F"
RED = "#E12B41"
ORANGE = "#FF9434"
GREEN = "#51BE4B"
INK = "#1B2333"
MUTED = "#5C6B84"

COMPANY = os.environ.get("JF_COMPANY", "CS Software Solutions")
SITE = os.environ.get("JF_SITE", "https://cssoftwaresolutions.in")
EMAIL = os.environ.get("JF_EMAIL", "")
WHATSAPP = os.environ.get("JF_WHATSAPP", "")
PRESS = os.environ.get("JF_PRESS", "Tri-Crest Press")
TAGLINE = "Innovate · Develop · Deliver"

_LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.png"


@lru_cache(maxsize=1)
def logo_b64() -> str:
    """Read and encode once per process; it is injected on every rerun."""
    try:
        return base64.b64encode(_LOGO.read_bytes()).decode()
    except Exception:
        return ""


CSS = f"""
<style>
  :root {{
    --navy: {NAVY}; --sky: {SKY}; --pink: {PINK};
    --ink: {INK}; --muted: {MUTED};
  }}
  .stApp {{ background: #FBFCFE; }}
  h1, h2, h3 {{ color: var(--navy); letter-spacing: -0.01em; }}

  .jf-bar {{
    height: 4px; border-radius: 4px; margin: 0 0 18px;
    background: linear-gradient(90deg, {NAVY}, {SKY}, {GREEN},
                {ORANGE}, {RED}, {PINK});
  }}
  .jf-head {{ display:flex; align-items:center; gap:16px; margin-bottom:4px; }}
  .jf-head img {{ width:64px; height:64px; border-radius:50%;
                  box-shadow:0 2px 10px rgba(8,35,138,.15); }}
  .jf-head h1 {{ margin:0; font-size:1.85rem; line-height:1.15; }}
  .jf-head .sub {{ color:var(--muted); font-size:.85rem; margin-top:2px; }}

  .jf-card {{
    background:#fff; border:1px solid #E6EAF2; border-radius:14px;
    padding:18px 20px; height:100%;
    box-shadow:0 1px 3px rgba(16,32,64,.05);
  }}
  .jf-card h4 {{ margin:.2rem 0 .3rem; color:var(--navy); font-size:1.05rem; }}
  .jf-card .ico {{ font-size:1.6rem; }}
  .jf-card .short {{ color:var(--sky); font-size:.78rem; font-weight:600;
                     text-transform:uppercase; letter-spacing:.04em; }}
  .jf-card p {{ color:var(--muted); font-size:.9rem; margin:.5rem 0 0; }}

  .jf-hero {{
    background:linear-gradient(135deg, {NAVY} 0%, #143FBF 55%, {SKY} 100%);
    color:#fff; border-radius:16px; padding:26px 30px; margin-bottom:18px;
  }}
  .jf-hero h2 {{ color:#fff; margin:0 0 6px; font-size:1.5rem; }}
  .jf-hero p {{ margin:0; opacity:.92; font-size:.95rem; }}

  .jf-foot {{ color:var(--muted); font-size:.85rem; }}
  .jf-foot a {{ color:var(--navy); text-decoration:none; font-weight:600; }}

  div.stButton > button[kind="primary"] {{
    background:{NAVY}; border:0; border-radius:9px; font-weight:600;
  }}
  div.stButton > button[kind="primary"]:hover {{ background:#0A2EB4; }}
  section[data-testid="stSidebar"] {{ background:#F4F7FC; }}
  /* hide Streamlit's auto page list — we render our own labelled nav below,
     otherwise the sidebar shows the raw filenames ("app", "1_Services") */
  section[data-testid="stSidebar"] div[data-testid="stSidebarNav"] {{ display:none; }}
</style>
"""


def apply() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def header(title: str = "Journal Formatter", subtitle: str = "") -> None:
    apply()
    b64 = logo_b64()
    img = (f'<img src="data:image/png;base64,{b64}" alt="{COMPANY}">'
           if b64 else "")
    st.markdown(
        f'<div class="jf-head">{img}<div>'
        f'<h1>{title}</h1>'
        f'<div class="sub">{subtitle or COMPANY} · {TAGLINE}</div>'
        f'</div></div><div class="jf-bar"></div>',
        unsafe_allow_html=True)


def hero(title: str, text: str) -> None:
    st.markdown(f'<div class="jf-hero"><h2>{title}</h2><p>{text}</p></div>',
                unsafe_allow_html=True)


def service_card(svc: dict) -> None:
    st.markdown(
        f'<div class="jf-card"><div class="ico">{svc["icon"]}</div>'
        f'<div class="short">{svc["short"]}</div>'
        f'<h4>{svc["title"]}</h4><p>{svc["blurb"]}</p></div>',
        unsafe_allow_html=True)


def contact_line() -> str:
    bits = [f'<a href="{SITE}" target="_blank">{COMPANY}</a>']
    if EMAIL:
        bits.append(f'<a href="mailto:{EMAIL}">{EMAIL}</a>')
    if WHATSAPP:
        digits = "".join(c for c in WHATSAPP if c.isdigit())
        bits.append(f'<a href="https://wa.me/{digits}" target="_blank">WhatsApp</a>')
    return " · ".join(bits)


def sidebar() -> None:
    with st.sidebar:
        b64 = logo_b64()
        if b64:
            st.markdown(
                f'<div style="text-align:center;margin-bottom:10px">'
                f'<img src="data:image/png;base64,{b64}" width="110" '
                f'style="border-radius:50%"></div>', unsafe_allow_html=True)
        st.markdown(f"**{COMPANY}**")
        st.caption("Research support for PhD scholars, postgraduates and "
                   "faculty worldwide.")
        st.page_link("app.py", label="Journal Formatter", icon="📄")
        st.page_link("pages/2_Journal_Finder.py", label="Journal Finder",
                     icon="🔍")
        st.page_link("pages/1_Services.py", label="Our Services", icon="🧩")
        st.markdown("---")
        st.markdown(contact_line(), unsafe_allow_html=True)
        st.caption("Uploads are processed in memory and deleted when you close "
                   "the page. Nothing you upload is stored.")


def footer() -> None:
    st.markdown('<div class="jf-bar" style="margin-top:26px"></div>',
                unsafe_allow_html=True)
    st.markdown(
        f'<div class="jf-foot">Built by {contact_line()} — statistical '
        f'analysis, manuscript preparation, code implementation and {PRESS} '
        f'book publishing.</div>', unsafe_allow_html=True)
