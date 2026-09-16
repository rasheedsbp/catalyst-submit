"""Top navigation bar, rendered on every page.

Streamlit's own sidebar page list shows raw filenames and hides on mobile, so
navigation lives at the top of the content area instead — where a visitor
arriving from a Google search actually looks.
"""
from __future__ import annotations

import streamlit as st

from . import theme

PAGES = [
    ("app.py", "Journal Formatter", "📄"),
    ("pages/2_Journal_Finder.py", "Journal Finder", "🔍"),
    ("pages/3_Conference_Finder.py", "Conference Finder", "🎤"),
    ("pages/4_Submission_Pack.py", "Submission Pack", "📦"),
    ("pages/1_Services.py", "Our Services", "🧩"),
]

NAV_CSS = f"""
<style>
  .jf-nav {{
    display:flex; gap:6px; align-items:center; flex-wrap:wrap;
    padding:8px 10px; margin:0 0 6px;
    background:#fff; border:1px solid #E6EAF2; border-radius:12px;
    box-shadow:0 1px 3px rgba(16,32,64,.05);
  }}
  .jf-nav .spacer {{ flex:1 }}
  .jf-nav a.cta {{
    background:{theme.NAVY}; color:#fff !important; padding:7px 16px;
    border-radius:8px; font-weight:600; font-size:.86rem;
    text-decoration:none; white-space:nowrap;
  }}
  .jf-nav a.cta:hover {{ background:#0A2EB4; }}
  /* Streamlit page links, restyled to sit inline like tabs */
  div[data-testid="stHorizontalBlock"] a[data-testid="stPageLink-NavLink"] {{
    border-radius:8px; padding:6px 12px; font-weight:600; font-size:.9rem;
  }}
  div[data-testid="stHorizontalBlock"] a[data-testid="stPageLink-NavLink"]:hover {{
    background:#EEF3FF;
  }}
</style>
"""


def bar(active: str = "") -> None:
    """Render the top navigation. `active` is the current page's file path."""
    st.markdown(NAV_CSS, unsafe_allow_html=True)
    cols = st.columns([1.4, 1.3, 1.5, 1.4, 1.2, 1.6])
    for col, (path, label, icon) in zip(cols, PAGES):
        with col:
            if path == active:
                st.markdown(
                    f'<div style="padding:6px 12px;border-radius:8px;'
                    f'background:#EEF3FF;color:{theme.NAVY};font-weight:700;'
                    f'font-size:.9rem">{icon} {label}</div>',
                    unsafe_allow_html=True)
            else:
                st.page_link(path, label=label, icon=icon)
    with cols[len(PAGES)]:
        st.markdown(
            f'<div style="text-align:right">'
            f'<a class="cta" href="{theme.SITE}" target="_blank">'
            f'Visit {theme.COMPANY} →</a></div>',
            unsafe_allow_html=True)
    st.markdown('<div class="jf-bar" style="margin:10px 0 16px"></div>',
                unsafe_allow_html=True)
