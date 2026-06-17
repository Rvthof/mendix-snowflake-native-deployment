"""Logs page: tail the service logs of a selected app."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from controller_client import ControllerError

st.set_page_config(page_title="Logs", layout="wide")
st.title("Service logs")

try:
    apps = client().list_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

if not apps:
    st.info("No apps registered yet.")
    st.stop()

names = [a["name"] for a in apps]
selected = st.selectbox("App", names)

cols = st.columns([1, 1, 4])
with cols[0]:
    lines = st.number_input("Lines", min_value=10, max_value=2000, value=200, step=50)
with cols[1]:
    auto = st.toggle("Auto-refresh", value=False, help="Refresh every 10 seconds.")


@st.fragment(run_every=10 if auto else None)
def _log_view() -> None:
    try:
        logs = client().get_logs(selected, lines=int(lines))
    except ControllerError as e:
        st.error(str(e))
        return
    if not logs:
        st.info("No log output (service may not be running yet).")
        return
    st.code(logs, language="log")


_log_view()
