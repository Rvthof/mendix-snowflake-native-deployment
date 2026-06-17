"""Apps page: list, inspect, and act on registered Mendix apps."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Streamlit's pages/ entry runs with the parent on sys.path automatically only
# for the entry script. Push the app/ dir on for sibling imports here too.
sys.path.append(str(Path(__file__).resolve().parent.parent))

import streamlit as st

from auth import client
from controller_client import ControllerError

st.set_page_config(page_title="Apps", layout="wide")
st.title("Apps")

_TRANSIENT = {"DEPLOYING", "SUSPENDING", "RESUMING", "STARTING"}


def _refresh_now() -> None:
    st.cache_data.clear()


@st.cache_data(ttl=5)
def _load_apps() -> list[dict]:
    return client().list_apps()


if st.button("Refresh"):
    _refresh_now()
    st.rerun()
st.caption("Status is fetched on page load and after each action. Click Refresh to re-poll.")

try:
    apps = _load_apps()
except ControllerError as e:
    st.error(f"Failed to load apps: {e}")
    st.stop()

if not apps:
    st.info("No apps registered yet. Use the Register page to add one.")
    st.stop()

table_rows = [
    {
        "name": a["name"],
        "service_status": a.get("service_status") or "",
        "last_deploy_status": a.get("last_deploy_status") or "",
        "endpoint_url": a.get("endpoint_url") or "",
        "last_deployed_at": a.get("last_deployed_at") or "",
    }
    for a in apps
]

selection = st.dataframe(
    table_rows,
    use_container_width=True,
    hide_index=True,
    selection_mode="single-row",
    on_select="rerun",
    column_config={
        "endpoint_url": st.column_config.LinkColumn("endpoint_url"),
    },
)

selected_rows = selection.selection.rows if selection and selection.selection else []
if not selected_rows:
    st.caption("Select a row to see details and actions.")
    st.stop()

selected_name = table_rows[selected_rows[0]]["name"]
app = next(a for a in apps if a["name"] == selected_name)

st.divider()


@st.fragment
def _detail_panel() -> None:
    try:
        live = client().get_app(selected_name)
    except ControllerError as e:
        st.error(f"Failed to refresh {selected_name}: {e}")
        return

    record = live["app"]
    svc_status = live.get("service_status") or "(unknown)"
    deploy_status = record.get("last_deploy_status") or "(none)"

    st.subheader(selected_name)
    c1, c2, c3 = st.columns(3)
    c1.metric("Service status", svc_status)
    c2.metric("Deploy status", deploy_status)
    c3.metric("Resource tier", record.get("resource_tier") or "")

    if record.get("endpoint_url"):
        st.write(f"Endpoint: {record['endpoint_url']}")
    st.write(f"Database: `{record.get('pg_database')}`  |  "
             f"Caller rights: `{record.get('use_caller_rights')}`  |  "
             f"Last deployed: `{record.get('last_deployed_at') or '—'}`")

    action_cols = st.columns(4)
    with action_cols[0]:
        if st.button("Redeploy", key=f"redeploy-{selected_name}",
                     disabled=deploy_status in _TRANSIENT, use_container_width=True):
            try:
                client().trigger_deploy(selected_name)
                _refresh_now()
                st.rerun()
            except ControllerError as e:
                st.error(str(e))
    with action_cols[1]:
        if st.button("Suspend", key=f"suspend-{selected_name}",
                     disabled=svc_status == "SUSPENDED" or deploy_status in _TRANSIENT,
                     use_container_width=True):
            try:
                client().suspend(selected_name)
                _refresh_now()
                st.rerun()
            except ControllerError as e:
                st.error(str(e))
    with action_cols[2]:
        if st.button("Resume", key=f"resume-{selected_name}",
                     disabled=svc_status == "RUNNING" or deploy_status in _TRANSIENT,
                     use_container_width=True):
            try:
                client().resume(selected_name)
                _refresh_now()
                st.rerun()
            except ControllerError as e:
                st.error(str(e))
    with action_cols[3]:
        with st.popover("Delete", use_container_width=True):
            st.warning(f"This will DROP service `{record.get('service_name')}` "
                       "and remove the registry entry. The PG database and stage are NOT deleted.")
            typed = st.text_input(
                f"Type `{selected_name}` to confirm:",
                key=f"delete-confirm-{selected_name}",
            )
            if st.button("Delete permanently", key=f"delete-go-{selected_name}",
                         type="primary", disabled=(typed != selected_name)):
                try:
                    client().delete_app(selected_name)
                    _refresh_now()
                    st.success(f"Deleted {selected_name}.")
                    st.rerun()
                except ControllerError as e:
                    st.error(str(e))

    with st.expander("Constants"):
        current = record.get("constants") or {}
        edited = st.text_area(
            "Constants (JSON object: name -> value)",
            value=json.dumps(current, indent=2),
            height=300,
            key=f"constants-{selected_name}",
        )
        if st.button("Save constants", key=f"save-constants-{selected_name}",
                     disabled=deploy_status in _TRANSIENT):
            try:
                parsed = json.loads(edited)
                if not isinstance(parsed, dict):
                    st.error("Constants must be a JSON object.")
                else:
                    client().update_constants(selected_name, parsed)
                    _refresh_now()
                    st.success("Constants update triggered. Service will restart.")
                    st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
            except ControllerError as e:
                st.error(str(e))


_detail_panel()
