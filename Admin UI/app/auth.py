"""Reads the authenticated operator's identity from the SPCS ingress header."""
from __future__ import annotations

import os

import streamlit as st

from controller_client import ControllerClient

_OPERATOR_HEADER = "Sf-Context-Current-User"
_ANONYMOUS = "<anonymous>"


def current_operator() -> str:
    """Return the Snowflake username injected by SPCS ingress."""
    try:
        headers = st.context.headers
    except Exception:
        return _ANONYMOUS
    return headers.get(_OPERATOR_HEADER) or _ANONYMOUS


def controller_url() -> str:
    return os.environ.get("CONTROLLER_URL", "http://mendix-deploy-controller:8080")


@st.cache_resource
def get_client(operator: str) -> ControllerClient:
    return ControllerClient(controller_url(), operator=operator)


def client() -> ControllerClient:
    return get_client(current_operator())
