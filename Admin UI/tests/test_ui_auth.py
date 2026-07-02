from __future__ import annotations

import auth as ui_auth


class TestPrivilegedRolesFn:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("PRIVILEGED_ROLES", raising=False)
        assert ui_auth._privileged_roles() == frozenset({"MENDIX_DEPLOY_CONTROLLER_ROLE"})

    def test_comma_parsing_and_case_folding(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", " role_a ,Role_B ")
        assert ui_auth._privileged_roles() == frozenset({"ROLE_A", "ROLE_B"})


class TestIsPrivilegedOperator:
    def test_true_when_roles_intersect(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", "PRIV")
        monkeypatch.setattr(ui_auth, "operator_roles", lambda: ("PRIV",))
        assert ui_auth.is_privileged_operator() is True

    def test_false_when_disjoint(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_ROLES", "PRIV")
        monkeypatch.setattr(ui_auth, "operator_roles", lambda: ("OTHER",))
        assert ui_auth.is_privileged_operator() is False


class TestControllerUrl:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONTROLLER_URL", "http://custom:9000")
        assert ui_auth.controller_url() == "http://custom:9000"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("CONTROLLER_URL", raising=False)
        assert ui_auth.controller_url() == "http://mendix-deploy-controller:8080"
