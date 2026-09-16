"""Bridge Streamlit Cloud secrets into the environment.

Everything in this project reads configuration with `os.environ`, which works
for a `.env` file under Docker and for shell variables locally. Streamlit
Community Cloud has neither — it exposes secrets through `st.secrets` instead.

Importing this module early copies any secrets into `os.environ`, so the same
code works in all three places without a special case. Existing environment
variables win, so a real env var still overrides a secret.
"""
from __future__ import annotations

import os


def load() -> None:
    try:
        import streamlit as st
        secrets = st.secrets
    except Exception:
        return
    try:
        items = list(secrets.items())
    except Exception:
        return
    for key, value in items:
        if isinstance(value, (str, int, float, bool)) and key not in os.environ:
            os.environ[key] = str(value)


load()
