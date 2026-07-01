from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .pad_parser import CONSTANT_NAME_PATTERN

_CONSTANT_NAME_RE = re.compile(CONSTANT_NAME_PATTERN)

# Sentinel returned in place of constant values everywhere they leave the
# controller (registry rows, API responses). Submitting it back means "keep the
# existing secret"; the literal string is therefore reserved and can never be
# stored as a real constant value.
HIDDEN_VALUE = "<HIDDEN>"


def _validate_constant_names(constants: dict[str, str]) -> dict[str, str]:
    # Constant names become Snowflake secret identifiers (MX_CONST_<name>), so a
    # name with quotes/spaces/semicolons could break out of the identifier
    # position in CREATE SECRET. Reject anything that is not a dotted identifier.
    for key in constants:
        if not _CONSTANT_NAME_RE.match(key):
            raise ValueError(
                f"invalid constant name {key!r}: must match {CONSTANT_NAME_PATTERN}"
            )
    return constants


class ResourceTier(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"


RESOURCE_TIERS = {
    ResourceTier.small:  {"cpu_request": "0.25", "cpu_limit": "0.5",  "mem_request": "512M", "mem_limit": "1G"},
    ResourceTier.medium: {"cpu_request": "0.5",  "cpu_limit": "1",    "mem_request": "1G",   "mem_limit": "2G"},
    ResourceTier.large:  {"cpu_request": "1",    "cpu_limit": "2",    "mem_request": "2G",   "mem_limit": "4G"},
}


class CreateAppRequest(BaseModel):
    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]*$", description="App identifier (letters, digits, underscores)")
    # Flows into the runtime's CREATE DATABASE (shell psql) and the service spec;
    # constrain to an identifier so it cannot inject SQL/shell metacharacters.
    pg_database: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    admin_password: str
    resource_tier: ResourceTier = ResourceTier.medium
    use_caller_rights: bool = False
    constants: dict[str, str] = Field(default_factory=dict)
    # Interpolated into GRANT … TO ROLE; constrain to an identifier so a privileged
    # caller can't inject SQL via this field (the UI restricts it, the API didn't).
    owner_role: str = Field(default="MENDIX_ADMIN_OPERATOR_ROLE", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")

    _check_constants = field_validator("constants")(_validate_constant_names)


class UpdateConstantsRequest(BaseModel):
    constants: dict[str, str]

    _check_constants = field_validator("constants")(_validate_constant_names)


class UpdateSpecRequest(BaseModel):
    resource_tier: Optional[ResourceTier] = None
    use_caller_rights: Optional[bool] = None


class AppRecord(BaseModel):
    name: str
    service_name: str
    pg_database: str
    resource_tier: str
    use_caller_rights: bool
    constants: dict[str, str]
    owner_role: str = "MENDIX_ADMIN_OPERATOR_ROLE"
    pad_stage_path: Optional[str]
    endpoint_url: Optional[str]
    last_deploy_status: Optional[str]
    created_at: Optional[str]
    last_deployed_at: Optional[str]


class AppStatusResponse(BaseModel):
    app: AppRecord
    service_status: Optional[str]


class UpdateComputePoolRequest(BaseModel):
    # Upper bounds cap runaway compute scaling (cost / compute-abuse guard). 10 nodes
    # of the pool's small instance family is ample headroom for Mendix workloads; raise
    # deliberately if a consumer genuinely needs more.
    min_nodes: Optional[int] = Field(None, ge=1, le=10)
    max_nodes: Optional[int] = Field(None, ge=1, le=10)
    auto_suspend_secs: Optional[int] = Field(None, ge=0)
