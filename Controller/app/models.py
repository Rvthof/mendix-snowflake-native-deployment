from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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
    pg_database: str
    admin_password: str
    resource_tier: ResourceTier = ResourceTier.medium
    use_caller_rights: bool = False
    constants: dict[str, str] = Field(default_factory=dict)
    # Interpolated into GRANT … TO ROLE; constrain to an identifier so a privileged
    # caller can't inject SQL via this field (the UI restricts it, the API didn't).
    owner_role: str = Field(default="MENDIX_ADMIN_OPERATOR_ROLE", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


class UpdateConstantsRequest(BaseModel):
    constants: dict[str, str]


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
    min_nodes: Optional[int] = Field(None, ge=1)
    max_nodes: Optional[int] = Field(None, ge=1)
    auto_suspend_secs: Optional[int] = Field(None, ge=0)
