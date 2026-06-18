"""Cached data loaders shared across admin UI pages."""
from __future__ import annotations

import streamlit as st

from auth import client, current_operator


@st.cache_data(ttl=60)
def _list_apps_cached(operator: str) -> list[dict]:
    # Keying by operator so per-operator filtering (v2 §5) works correctly
    # once the controller starts honoring X-Operator-Roles.
    return client().list_apps()


def list_apps() -> list[dict]:
    return _list_apps_cached(current_operator())
