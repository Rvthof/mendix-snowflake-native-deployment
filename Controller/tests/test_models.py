from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    RESOURCE_TIERS,
    CreateAppRequest,
    ResourceTier,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
)


def _make(**overrides):
    defaults = dict(name="myapp", pg_database="myapp_db", admin_password="pw")
    defaults.update(overrides)
    return CreateAppRequest(**defaults)


class TestCreateAppRequest:
    def test_valid_minimal_payload(self):
        req = _make()
        assert req.name == "myapp"
        assert req.resource_tier == ResourceTier.medium
        assert req.use_caller_rights is False
        assert req.owner_role == "MENDIX_ADMIN_OPERATOR_ROLE"

    def test_name_with_hyphen_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="my-app")

    def test_name_with_leading_digit_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="1app")

    def test_name_with_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="app;drop")

    def test_pg_database_with_hyphen_rejected(self):
        with pytest.raises(ValidationError):
            _make(pg_database="my-db")

    def test_pg_database_with_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(pg_database="db;drop")

    def test_owner_role_sql_injection_rejected(self):
        with pytest.raises(ValidationError):
            _make(owner_role="X'; DROP TABLE users; --")


class TestValidateConstantNames:
    def test_dotted_name_accepted(self):
        req = _make(constants={"MyModule.MyConst": "value"})
        assert req.constants == {"MyModule.MyConst": "value"}

    def test_quote_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad'name": "v"})

    def test_space_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad name": "v"})

    def test_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad;name": "v"})

    def test_update_constants_request_validates_names(self):
        with pytest.raises(ValidationError):
            UpdateConstantsRequest(constants={"bad;name": "v"})


class TestUpdateComputePoolRequest:
    def test_min_nodes_zero_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(min_nodes=0)

    def test_max_nodes_eleven_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(max_nodes=11)

    def test_auto_suspend_secs_negative_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(auto_suspend_secs=-1)

    def test_all_none_allowed(self):
        req = UpdateComputePoolRequest()
        assert req.min_nodes is None
        assert req.max_nodes is None
        assert req.auto_suspend_secs is None


def test_resource_tiers_has_exactly_three_keys():
    assert set(RESOURCE_TIERS.keys()) == set(ResourceTier)
    assert len(RESOURCE_TIERS) == 3
