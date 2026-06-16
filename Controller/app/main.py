from __future__ import annotations

import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from . import registry, snowflake_client as sf
from .models import (
    AppRecord,
    AppStatusResponse,
    CreateAppRequest,
    DeployResponse,
    MissingConstantsError,
    RESOURCE_TIERS,
    ResourceTier,
    UpdateConstantsRequest,
)
from .pad_parser import PadConstant, parse_from_zip

app = FastAPI(title="Mendix SPCS Deployment Controller")

DB_SCHEMA = os.environ["DB_SCHEMA"]
COMPUTE_POOL = os.environ["COMPUTE_POOL"]
IMAGE_REPO = os.environ["IMAGE_REPO"]
PG_EAI = os.environ["PG_EAI"]
QUERY_WAREHOUSE = os.environ["QUERY_WAREHOUSE"]
DEPLOY_STAGE = f"@{DB_SCHEMA}.MENDIX_DEPLOY_STAGE"
DEPLOY_STAGE_MOUNT = "/mnt/deploy-stage"

# Derived from controller secrets at startup
_PG_HOST: str | None = None


def _pg_host() -> str:
    global _PG_HOST
    if _PG_HOST is None:
        secret_file = "/secrets/pg_host/secret_string"
        if os.path.exists(secret_file):
            _PG_HOST = open(secret_file).read().strip()
        else:
            _PG_HOST = os.environ.get("PG_HOST", "localhost:5432")
    return _PG_HOST


def _service_name(app_name: str) -> str:
    return f"{app_name.upper()}_SERVICE"


def _filestorage_stage(app_name: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_FILESTORAGE_STAGE"


def _secret_fqn(app_name: str, suffix: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_{suffix.upper()}"


def _const_secret_fqn(app_name: str, secret_name: str) -> str:
    return f"{DB_SCHEMA}.{app_name.upper()}_{secret_name}"


def _build_spec(
    app_name: str,
    pg_database: str,
    resource_tier: ResourceTier,
    constants: list[PadConstant],
    use_caller_rights: bool,
) -> str:
    res = RESOURCE_TIERS[resource_tier]
    pg_host_port = _pg_host()
    image_path = f"/{IMAGE_REPO}:latest"
    pad_path = f"{DEPLOY_STAGE_MOUNT}/apps/{app_name}/current.zip"

    secret_entries = [
        {
            "snowflakeSecret": _secret_fqn(app_name, "PG_PASS"),
            "directoryPath": "/secrets/pg_pass",
        },
        {
            "snowflakeSecret": _secret_fqn(app_name, "ADMIN_PASS"),
            "directoryPath": "/secrets/admin_pass",
        },
    ]
    for c in constants:
        secret_entries.append({
            "snowflakeSecret": _const_secret_fqn(app_name, c.secret_name),
            "directoryPath": f"/secrets/{c.secret_name.lower()}",
        })

    spec: dict = {
        "spec": {
            "containers": [{
                "name": "mendix-app",
                "image": image_path,
                "env": {
                    "PAD_STAGE_PATH": pad_path,
                    "RUNTIME_PARAMS_DATABASETYPE": "POSTGRESQL",
                    "RUNTIME_PARAMS_DATABASEHOST": pg_host_port,
                    "RUNTIME_PARAMS_DATABASENAME": pg_database,
                    "RUNTIME_PARAMS_DATABASEUSERNAME": "application",
                    "RUNTIME_PARAMS_DATABASEUSESSL": "true",
                    "RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE": "com.mendix.storage.localfilesystem",
                    "RUNTIME_PARAMS_UPLOADEDFILESPATH": "/mnt/filestorage",
                },
                "secrets": secret_entries,
                "readinessProbe": {"port": 8080, "path": "/"},
                "resources": {
                    "requests": {"memory": res["mem_request"], "cpu": res["cpu_request"]},
                    "limits":   {"memory": res["mem_limit"],   "cpu": res["cpu_limit"]},
                },
                "volumeMounts": [
                    {"name": "filestorage",   "mountPath": "/mnt/filestorage"},
                    {"name": "deploy-stage",  "mountPath": DEPLOY_STAGE_MOUNT},
                ],
            }],
            "volumes": [
                {
                    "name": "filestorage",
                    "source": "stage",
                    "stageConfig": {"name": f"@{_filestorage_stage(app_name)}"},
                },
                {
                    "name": "deploy-stage",
                    "source": "stage",
                    "stageConfig": {"name": DEPLOY_STAGE},
                },
            ],
            "endpoints": [{"name": "mendix-web", "port": 8080, "public": True}],
        }
    }

    if use_caller_rights:
        spec["capabilities"] = {"securityContext": {"executeAsCaller": True}}

    return yaml.dump(spec, default_flow_style=False, sort_keys=False)


def _poll_status(service_name: str, target: str, timeout_secs: int = 300) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        status = sf.show_service_status(service_name)
        if status == target:
            return True
        time.sleep(5)
    return False


def _sync_constant_secrets(app_name: str, constants: list[PadConstant], values: dict[str, str]) -> None:
    for c in constants:
        val = values.get(c.name, c.default)
        sf.create_or_replace_secret(_const_secret_fqn(app_name, c.secret_name), val)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/apps")
def list_apps():
    apps = registry.list_apps()
    result = []
    for a in apps:
        svc_status = sf.show_service_status(a.service_name)
        result.append({**a.model_dump(), "service_status": svc_status})
    return result


@app.post("/apps", status_code=status.HTTP_201_CREATED)
def create_app(req: CreateAppRequest):
    if registry.get_app(req.name):
        raise HTTPException(status_code=409, detail=f"App '{req.name}' already exists")

    service_name = _service_name(req.name)
    filestorage_fqn = _filestorage_stage(req.name)

    # Create filestorage stage
    sf.create_stage(filestorage_fqn)

    # Create PG password and admin password secrets
    sf.create_or_replace_secret(_secret_fqn(req.name, "PG_PASS"), req.pg_database)
    sf.create_or_replace_secret(_secret_fqn(req.name, "ADMIN_PASS"), req.admin_password)

    # Create constant secrets from provided values (using defaults for any not supplied)
    constants: list[PadConstant] = []  # no PAD yet at create time
    for name, value in req.constants.items():
        from .pad_parser import PadConstant as PC
        secret_name = "MX_CONST_" + name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(req.name, secret_name), value)
        constants.append(PC(name=name, env_var="", default=value, secret_name=secret_name))

    spec = _build_spec(req.name, req.pg_database, req.resource_tier, constants, req.use_caller_rights)

    sf.create_service(service_name, spec, COMPUTE_POOL, PG_EAI, QUERY_WAREHOUSE)

    if req.use_caller_rights:
        sf.set_caller_token_validity(service_name, 1800)

    endpoint_url = sf.get_service_endpoint(service_name)

    record = AppRecord(
        name=req.name,
        service_name=service_name,
        pg_database=req.pg_database,
        resource_tier=req.resource_tier,
        use_caller_rights=req.use_caller_rights,
        constants=req.constants,
        pad_stage_path=None,
        endpoint_url=endpoint_url,
        last_deploy_status="STARTING",
        created_at=None,
        last_deployed_at=None,
    )
    registry.create_app(record)

    return {"service_name": service_name, "endpoint_url": endpoint_url, "status": "STARTING"}


@app.get("/apps/{name}")
def get_app(name: str):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    svc_status = sf.show_service_status(record.service_name)
    return AppStatusResponse(app=record, service_status=svc_status)


@app.post("/apps/{name}/deploy")
def deploy_pad(name: str, pad_file: UploadFile = File(...)):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})

    # Save upload to temp file
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(pad_file.file, tmp)
        tmp_path = tmp.name

    try:
        # Parse constants from the uploaded PAD
        pad_constants = parse_from_zip(tmp_path)

        # Check for constants with no stored value
        stored = record.constants or {}
        missing = [c.name for c in pad_constants if c.name not in stored and not c.default]
        if missing:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return JSONResponse(
                status_code=422,
                content={"detail": "New constants with no value", "missing": missing},
            )

        # Write PAD to the mounted deploy stage (direct file I/O — stage is mounted at DEPLOY_STAGE_MOUNT)
        dest_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, "current.zip")
        shutil.copy2(tmp_path, dest_path)

        # Determine if any constant values changed
        new_constants = {**stored}
        for c in pad_constants:
            if c.name not in new_constants:
                new_constants[c.name] = c.default

        constants_changed = any(
            new_constants.get(c.name) != stored.get(c.name)
            for c in pad_constants
        )

        if constants_changed:
            _sync_constant_secrets(name, pad_constants, new_constants)
            spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                               pad_constants, record.use_caller_rights)
            sf.alter_service_spec(record.service_name, spec)
        else:
            # Suspend → poll → resume
            sf.suspend_service(record.service_name)
            if not _poll_status(record.service_name, "SUSPENDED", timeout_secs=120):
                raise RuntimeError(f"Service {record.service_name} did not suspend within 120s")
            sf.resume_service(record.service_name)

        # Poll until RUNNING
        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            logs = sf.get_service_logs(record.service_name)
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            raise HTTPException(status_code=500, detail=f"Service did not reach RUNNING state.\n{logs[-2000:]}")

        pad_stage_path = f"apps/{name}/current.zip"
        registry.update_app(name, {
            "constants": new_constants,
            "pad_stage_path": pad_stage_path,
            "last_deploy_status": "READY",
            "last_deployed_at": datetime.now(timezone.utc).isoformat(),
        })

        endpoint_url = record.endpoint_url or sf.get_service_endpoint(record.service_name)
        return DeployResponse(endpoint_url=endpoint_url, status="READY")

    finally:
        os.unlink(tmp_path)


@app.put("/apps/{name}/constants")
def update_constants(name: str, req: UpdateConstantsRequest):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    from .pad_parser import PadConstant as PC
    constants: list[PadConstant] = []
    for const_name, value in req.constants.items():
        secret_name = "MX_CONST_" + const_name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(name, secret_name), value)
        constants.append(PC(name=const_name, env_var="", default=value, secret_name=secret_name))

    merged = {**(record.constants or {}), **req.constants}
    spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                       constants, record.use_caller_rights)
    sf.alter_service_spec(record.service_name, spec)

    registry.update_app(name, {"constants": merged, "last_deploy_status": "DEPLOYING"})

    if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
        registry.update_app(name, {"last_deploy_status": "FAILED"})
        raise HTTPException(status_code=500, detail="Service did not reach RUNNING state after constants update")

    registry.update_app(name, {"last_deploy_status": "READY"})
    return {"status": "READY"}


@app.delete("/apps/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(name: str):
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    try:
        sf.suspend_service(record.service_name)
        _poll_status(record.service_name, "SUSPENDED", timeout_secs=60)
    except Exception:
        pass

    sf.drop_service(record.service_name)
    registry.delete_app(name)
