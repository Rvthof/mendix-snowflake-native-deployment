from __future__ import annotations

import pytest


class TestSystemLogs:
    def test_unknown_target_404(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/nonsense", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_get_service_logs_failure_502(self, client, fake_sf, role_headers):
        fake_sf.raise_on["get_service_logs"] = RuntimeError("access denied")
        resp = client.get("/system/logs/controller", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 502
        assert "access denied" in resp.json()["detail"]

    def test_valid_target_returns_logs(self, client, fake_sf, role_headers):
        fake_sf.logs = "the log body"
        resp = client.get("/system/logs/controller", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == {"logs": "the log body"}
        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_CONTROLLER"
        assert kwargs["container"] == "controller"

    def test_admin_ui_target_uses_streamlit_container(self, client, fake_sf, role_headers):
        resp = client.get("/system/logs/admin-ui", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        args, kwargs = fake_sf.calls_for("get_service_logs")[0]
        assert args[0] == "MENDIX_DEPLOY_ADMIN_UI"
        assert kwargs["container"] == "streamlit"


class TestGetComputePool:
    def test_none_pool_404(self, client, fake_sf, role_headers):
        fake_sf.compute_pool = None
        resp = client.get("/system/compute-pool", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 404

    def test_present_pool_passthrough(self, client, fake_sf, role_headers):
        resp = client.get("/system/compute-pool", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert resp.json() == fake_sf.compute_pool


class TestUpdateComputePool:
    # BUG (found by this suite; not fixed, per scope: no app-code changes):
    # activity.derive_action's "/system/compute-pool" regex has no capture
    # group, but derive_action unconditionally calls m.group(1). main.py's
    # log_operator middleware calls derive_action for every mutating request
    # (PATCH included) *after* call_next has already produced the response,
    # so every PATCH /system/compute-pool - regardless of whether the route
    # itself would 400/422/202 - raises an unhandled IndexError in the
    # middleware. With the default TestClient(raise_server_exceptions=True)
    # that exception propagates to the caller instead of any HTTP response.
    # See test_activity_unit.py::TestDeriveAction::test_resize_compute_pool.
    def test_all_none_body_raises_middleware_bug(self, client, fake_sf, role_headers):
        with pytest.raises(IndexError):
            client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"), json={})

    def test_partial_body_raises_middleware_bug(self, client, fake_sf, role_headers):
        with pytest.raises(IndexError):
            client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"),
                         json={"min_nodes": 2})

    def test_out_of_bounds_still_422_before_middleware_runs(self, client, fake_sf, role_headers):
        # Pydantic validation (422) happens before the route body executes, and
        # a 422 response still counts as a "mutation" for the middleware, so
        # this ALSO trips the same bug rather than cleanly returning 422.
        with pytest.raises(IndexError):
            client.patch("/system/compute-pool", headers=role_headers("PRIV_ROLE"),
                         json={"min_nodes": 99})
