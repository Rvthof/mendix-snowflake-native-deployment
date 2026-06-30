from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from . import activity, auth, registry, snowflake_client as sf
from .models import (
    AppRecord,
    AppStatusResponse,
    CreateAppRequest,
    RESOURCE_TIERS,
    ResourceTier,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
    UpdateSpecRequest,
)
from .pad_parser import PadConstant, parse_from_zip


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        activity.init_table()
    except Exception:
        logger.exception("Failed to initialise MENDIX_ACTIVITY")
    yield


app = FastAPI(title="Mendix SPCS Deployment Controller", lifespan=lifespan)


@app.middleware("http")
async def log_operator(request: Request, call_next):
    is_mutation = request.method in ("POST", "PUT", "PATCH", "DELETE")
    response = await call_next(request)
    if is_mutation:
        # Identify the operator. The admin UI sets X-Operator; PAT clients
        # (upload-pad.ps1) don't, so resolve the real Snowflake user from the
        # caller token (a cache hit, since the route dependency already resolved it).
        operator = request.headers.get("X-Operator")
        if not operator:
            try:
                operator = auth.resolve_caller(request).user
            except Exception:
                operator = None
        operator = operator or "<anonymous>"
        action, app_name = activity.derive_action(request.method, request.url.path)
        # Record the real outcome: 2xx accepted the call, anything else was rejected
        # (authorization, validation, or a synchronous error). Background deploy
        # outcomes are tracked separately in the registry's last_deploy_status.
        result = "accepted" if response.status_code < 400 else f"rejected ({response.status_code})"
        logger.info("operator=%s %s %s -> %s", operator, request.method, request.url.path, response.status_code)
        try:
            activity.insert(
                operator=operator,
                action=action,
                app_name=app_name,
                detail={"path": request.url.path, "method": request.method, "status": response.status_code},
                result=result,
            )
        except Exception:
            logger.exception("Failed to record activity row")
    return response

DB_SCHEMA = os.environ["DB_SCHEMA"]
COMPUTE_POOL = os.environ["COMPUTE_POOL"]
IMAGE_REPO = os.environ["IMAGE_REPO"]
PG_EAI = os.environ["PG_EAI"]
QUERY_WAREHOUSE = os.environ["QUERY_WAREHOUSE"]
DEPLOY_STAGE = f"@{DB_SCHEMA}.MENDIX_DEPLOY_STAGE"
DEPLOY_STAGE_MOUNT = "/mnt/deploy-stage"

# Infrastructure services whose own logs are exposed via /system/logs/{target}.
# (service_name, container). Defaults match the names created by the setup scripts;
# override via env if a deployment renames them.
CONTROLLER_SERVICE_NAME = os.environ.get("CONTROLLER_SERVICE_NAME", "MENDIX_DEPLOY_CONTROLLER")
ADMIN_UI_SERVICE_NAME = os.environ.get("ADMIN_UI_SERVICE_NAME", "MENDIX_DEPLOY_ADMIN_UI")
SYSTEM_SERVICES: dict[str, tuple[str, str]] = {
    "controller": (CONTROLLER_SERVICE_NAME, "controller"),
    "admin-ui": (ADMIN_UI_SERVICE_NAME, "streamlit"),
}

# Derived from the bound pg_secret at startup
_PG_HOST: str | None = None
_PG_PASSWORD: str | None = None


def _load_pg_credentials() -> tuple[str, str]:
    """Read the bound pg_secret (GENERIC_STRING) mounted at /secrets/pg.

    The secret string is JSON: {"host": "<host:port>", "password": "<pw>"}.
    Both values are cached after the first read. Falls back to PG_HOST / PG_PASS
    env vars for local development outside SPCS.
    """
    global _PG_HOST, _PG_PASSWORD
    if _PG_HOST is None or _PG_PASSWORD is None:
        secret_file = "/secrets/pg/secret_string"
        if os.path.exists(secret_file):
            with open(secret_file) as f:
                raw = f.read()
            try:
                data = json.loads(raw)
                _PG_HOST = str(data["host"]).strip()
                _PG_PASSWORD = str(data["password"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                raise RuntimeError(
                    "pg_secret at /secrets/pg/secret_string must be JSON with "
                    '"host" and "password" keys, e.g. '
                    '{"host": "<host:port>", "password": "<pw>"}'
                ) from e
        else:
            _PG_HOST = os.environ.get("PG_HOST", "localhost:5432")
            _PG_PASSWORD = os.environ.get("PG_PASS", "")
    return _PG_HOST, _PG_PASSWORD


def _pg_host() -> str:
    return _load_pg_credentials()[0]


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
        time.sleep(10)
    return False


def _sync_constant_secrets(app_name: str, constants: list[PadConstant], values: dict[str, str]) -> None:
    for c in constants:
        val = values.get(c.name, c.default)
        sf.create_or_replace_secret(_const_secret_fqn(app_name, c.secret_name), val)


def _constants_from_dict(d: dict[str, str]) -> list[PadConstant]:
    return [
        PadConstant(name=k, env_var="", default=v,
                    secret_name="MX_CONST_" + k.replace(".", "_").upper())
        for k, v in d.items()
    ]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def caller_roles(request: Request) -> set[str]:
    """FastAPI dependency: the authoritative role set for the request."""
    return auth.resolve_caller_roles(request)


def _record_for_read(name: str, roles: set[str]) -> AppRecord:
    """Load an app the caller may see, else 404 (unauthorized is indistinguishable
    from missing, so existence is not leaked)."""
    record = registry.get_app(name)
    if not record or not auth.authorize(record.owner_role, roles):
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    return record


def _record_for_mutation(name: str, roles: set[str]) -> AppRecord:
    """Load an app the caller may mutate: 404 if missing, 403 if not authorized."""
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")
    if not auth.authorize(record.owner_role, roles):
        raise HTTPException(status_code=403, detail=f"Not authorized for app '{name}'")
    return record


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


def _endpoint_is_real(url: str | None) -> bool:
    """A real ingress host has a dot and no spaces; the provisioning placeholder
    ("Endpoints provisioning in progress. ...") has spaces."""
    return bool(url) and " " not in url and "." in url


def _effective_endpoint(record: AppRecord, svc_status: str | None) -> str | None:
    """Return the app's endpoint, healing a stale/empty stored value.

    endpoint_url is captured once at deploy time, but SPCS provisions ingress
    asynchronously after the service reports RUNNING, so that capture is usually
    empty (or, from older builds, a stored provisioning message). When the
    service is RUNNING and we have no real stored endpoint, fetch the live one
    and persist it so later reads stay cheap."""
    if _endpoint_is_real(record.endpoint_url):
        return record.endpoint_url
    if svc_status == "RUNNING":
        live = sf.get_service_endpoint(record.service_name)
        if live:
            registry.update_app(record.name, {"endpoint_url": live})
            return live
    return None


@app.get("/apps")
def list_apps(roles: set[str] = Depends(caller_roles)):
    apps = registry.list_apps()
    statuses = sf.show_all_service_statuses()
    result = []
    for a in apps:
        if not auth.authorize(a.owner_role, roles):
            continue
        svc_status = statuses.get(a.service_name)
        result.append({
            **a.model_dump(),
            "service_status": svc_status,
            "endpoint_url": _effective_endpoint(a, svc_status),
        })
    return result


@app.post("/apps", status_code=status.HTTP_201_CREATED)
def create_app(req: CreateAppRequest, roles: set[str] = Depends(caller_roles)):
    if not auth.authorize(req.owner_role, roles):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot assign owner_role '{req.owner_role}': not one of your roles",
        )
    if registry.get_app(req.name):
        raise HTTPException(status_code=409, detail=f"App '{req.name}' already exists")

    service_name = _service_name(req.name)
    filestorage_fqn = _filestorage_stage(req.name)

    # Create filestorage stage
    sf.create_stage(filestorage_fqn)

    # Create PG password and admin password secrets.
    # Read the bootstrap PG password from the controller's bound pg_secret (/secrets/pg).
    # req.pg_database is the target database name, not the password.
    _, pg_password = _load_pg_credentials()
    if not pg_password:
        raise HTTPException(status_code=500, detail="Controller PG credentials not mounted at /secrets/pg")
    sf.create_or_replace_secret(_secret_fqn(req.name, "PG_PASS"), pg_password)
    sf.create_or_replace_secret(_secret_fqn(req.name, "ADMIN_PASS"), req.admin_password)

    # Create constant secrets from provided values (using defaults for any not supplied)
    constants: list[PadConstant] = []  # no PAD yet at create time
    for const_name, value in req.constants.items():
        secret_name = "MX_CONST_" + const_name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(req.name, secret_name), value)
        constants.append(PadConstant(name=const_name, env_var="", default=value, secret_name=secret_name))

    spec = _build_spec(req.name, req.pg_database, req.resource_tier, constants, req.use_caller_rights)

    sf.create_service(service_name, spec, COMPUTE_POOL, PG_EAI, QUERY_WAREHOUSE)

    if req.use_caller_rights:
        sf.set_caller_token_validity(service_name, 1800)

    # Data-plane access control (PLAN-app-access-control.md A1+B1;
    # PLAN-native-app-packaging.md section 6): gate the public endpoint behind a
    # per-app APPLICATION role. End-user membership of app_<name>_user is managed
    # in the IdP via SCIM (GRANT APPLICATION ROLE ... TO USER). Also grant app_admin
    # so any operator can reach the app before the IdP group is populated (owner
    # bootstrap; replaces the old owner_role grant - an application cannot grant its
    # service role to a consumer account role).
    sf.create_app_access_role(req.name)
    sf.grant_endpoint_to_app_role(service_name, sf.app_access_role_name(req.name))
    sf.grant_endpoint_to_app_role(service_name, sf.APP_ADMIN_ROLE)

    # Endpoint URL is not available until the service starts; it's captured by _run_deploy.
    record = AppRecord(
        name=req.name,
        service_name=service_name,
        pg_database=req.pg_database,
        resource_tier=req.resource_tier,
        use_caller_rights=req.use_caller_rights,
        constants=req.constants,
        pad_stage_path=None,
        endpoint_url=None,
        # Non-transient: the app has no PAD yet. A transient status here would
        # disable the Redeploy action that performs the first deploy (deadlock).
        last_deploy_status="NOT_DEPLOYED",
        created_at=None,
        last_deployed_at=None,
        owner_role=req.owner_role,
    )
    registry.create_app(record)

    return {"service_name": service_name, "status": "NOT_DEPLOYED"}


@app.get("/apps/{name}")
def get_app(name: str, roles: set[str] = Depends(caller_roles)):
    record = _record_for_read(name, roles)
    svc_status = sf.show_service_status(record.service_name)
    record.endpoint_url = _effective_endpoint(record, svc_status)
    return AppStatusResponse(app=record, service_status=svc_status)


@app.get("/apps/{name}/logs")
def get_logs(name: str, lines: int = 200, roles: set[str] = Depends(caller_roles)):
    record = _record_for_read(name, roles)
    logs = sf.get_service_logs(record.service_name, lines=lines)
    return {"logs": logs}


@app.get("/system/logs/{target}")
def get_system_logs(target: str, lines: int = 200, roles: set[str] = Depends(caller_roles)):
    """Logs for the infrastructure services themselves (controller, admin UI).

    Restricted to privileged roles: these logs span every tenant's operator
    activity, so they sit outside the per-app owner_role isolation.
    """
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="System logs are restricted to privileged roles")
    entry = SYSTEM_SERVICES.get(target)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown system service '{target}'")
    service_name, container = entry
    try:
        logs = sf.get_service_logs(service_name, container=container, lines=lines)
    except Exception as e:
        # Surface the underlying reason (e.g. the controller's role lacks access to
        # another service's logs) instead of an opaque 500.
        raise HTTPException(status_code=502, detail=f"Could not read {target} logs: {e}")
    return {"logs": logs}


@app.get("/system/compute-pool")
def get_compute_pool(roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    pool = sf.get_compute_pool(COMPUTE_POOL)
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Compute pool '{COMPUTE_POOL}' not found")
    return pool


@app.patch("/system/compute-pool")
def update_compute_pool(req: UpdateComputePoolRequest, roles: set[str] = Depends(caller_roles)):
    if not (roles & auth.PRIVILEGED_ROLES):
        raise HTTPException(status_code=403, detail="Restricted to privileged roles")
    if req.min_nodes is None and req.max_nodes is None and req.auto_suspend_secs is None:
        raise HTTPException(status_code=400, detail="At least one field must be provided")
    sf.alter_compute_pool(
        COMPUTE_POOL,
        min_nodes=req.min_nodes,
        max_nodes=req.max_nodes,
        auto_suspend_secs=req.auto_suspend_secs,
    )
    pool = sf.get_compute_pool(COMPUTE_POOL)
    return pool or {}


def _prepare_deploy(
    name: str, pad_path: str
) -> tuple[AppRecord, list[PadConstant], dict]:
    """Parse and validate a PAD. Returns (record, pad_constants, new_constants). Raises HTTPException on error."""
    record = registry.get_app(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"App '{name}' not found")

    pad_constants = parse_from_zip(pad_path)
    stored = record.constants or {}
    missing = [c.name for c in pad_constants if c.name not in stored and not c.default]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"detail": "New constants with no value", "missing": missing},
        )

    new_constants = {**stored}
    for c in pad_constants:
        if c.name not in new_constants:
            new_constants[c.name] = c.default

    return record, pad_constants, new_constants


def _stamp_deploy_success(name: str, service_name: str, extra: dict | None = None) -> None:
    """Record a successful deploy/restart: capture the live endpoint, stamp the deploy
    time, and mark the app READY. Shared by every background task that restarts a
    service so they all populate endpoint_url + last_deployed_at (a constants-only
    deploy used to leave both empty)."""
    update = {
        "endpoint_url": sf.get_service_endpoint(service_name),
        "last_deploy_status": "READY",
        "last_deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        update.update(extra)
    registry.update_app(name, update)


def _run_deploy(name: str, pad_path: str, record: AppRecord,
                pad_constants: list[PadConstant], new_constants: dict) -> None:
    """Background deploy task. registry status must be set to DEPLOYING before calling."""
    try:
        stored = record.constants or {}
        constants_changed = any(
            new_constants.get(c.name) != stored.get(c.name)
            for c in pad_constants
        )

        if constants_changed:
            _sync_constant_secrets(name, pad_constants, new_constants)
            # Persist constants alongside the secret sync so a failed restart cannot
            # leave the registry and the per-app secrets out of step (see the same
            # fix in _run_update_constants).
            registry.update_app(name, {"constants": new_constants})
            spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                               _constants_from_dict(new_constants), record.use_caller_rights)
            sf.alter_service_spec(record.service_name, spec)
        else:
            sf.suspend_service(record.service_name)
            if not _poll_status(record.service_name, "SUSPENDED", timeout_secs=120):
                raise RuntimeError(f"Service {record.service_name} did not suspend within 120s")
            sf.resume_service(record.service_name)

        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return

        _stamp_deploy_success(name, record.service_name, {
            "constants": new_constants,
            "pad_stage_path": f"apps/{name}/current.zip",
        })
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Deploy failed for %s", name)


@app.post("/apps/{name}/deploy", status_code=202)
def deploy_pad(name: str, pad_file: UploadFile = File(...),
               background_tasks: BackgroundTasks = None,
               roles: set[str] = Depends(caller_roles)):
    """Upload a PAD zip. For large PADs (>50 MB) use snow stage copy + /trigger-deploy instead."""
    _record_for_mutation(name, roles)
    dest_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "current.zip")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(pad_file.file, tmp)
        tmp_path = tmp.name
    shutil.copy2(tmp_path, dest_path)
    os.unlink(tmp_path)

    record, pad_constants, new_constants = _prepare_deploy(name, dest_path)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_deploy, name, dest_path, record, pad_constants, new_constants)
    return {"status": "DEPLOYING"}


def _resolve_staged_pad(name: str) -> str | None:
    """Find the PAD a consumer staged under apps/<name>/.

    Prefer current.zip (the canonical name). Otherwise accept the newest .zip in
    the directory, so the documented `snow stage copy <yourpad>.zip @.../apps/<name>/`
    one-liner works without forcing the consumer to rename the file first.
    """
    app_dir = os.path.join(DEPLOY_STAGE_MOUNT, "apps", name)
    canonical = os.path.join(app_dir, "current.zip")
    if os.path.isfile(canonical):
        return canonical
    if not os.path.isdir(app_dir):
        return None
    zips = [
        os.path.join(app_dir, f)
        for f in os.listdir(app_dir)
        if f.lower().endswith(".zip") and os.path.isfile(os.path.join(app_dir, f))
    ]
    if not zips:
        return None
    return max(zips, key=os.path.getmtime)


@app.post("/apps/{name}/trigger-deploy", status_code=202)
def trigger_deploy(name: str, background_tasks: BackgroundTasks,
                   roles: set[str] = Depends(caller_roles)):
    """Trigger deploy from a PAD already staged under apps/{name}/ (current.zip or newest .zip)."""
    _record_for_mutation(name, roles)
    pad_path = _resolve_staged_pad(name)
    if pad_path is None:
        raise HTTPException(
            status_code=400,
            detail=f"No PAD (.zip) found at stage path apps/{name}/ — upload it first.",
        )
    record, pad_constants, new_constants = _prepare_deploy(name, pad_path)
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_deploy, name, pad_path, record, pad_constants, new_constants)
    return {"status": "DEPLOYING"}


def _run_update_constants(name: str, service_name: str, merged: dict,
                          record: AppRecord, constants: list[PadConstant]) -> None:
    """Background task for constants update."""
    # Persist constants up front, independent of the restart outcome: the per-app
    # secrets are already written by the endpoint handler, so a failed restart must
    # not discard the registry copy (otherwise the UI shows constants as {}).
    registry.update_app(name, {"constants": merged})
    try:
        spec = _build_spec(name, record.pg_database, ResourceTier(record.resource_tier),
                           constants, record.use_caller_rights)
        sf.alter_service_spec(service_name, spec)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        _stamp_deploy_success(name, service_name)
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Constants update failed for %s", name)


@app.put("/apps/{name}/constants", status_code=202)
def update_constants(name: str, req: UpdateConstantsRequest, background_tasks: BackgroundTasks,
                     roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)

    for const_name, value in req.constants.items():
        secret_name = "MX_CONST_" + const_name.replace(".", "_").upper()
        sf.create_or_replace_secret(_const_secret_fqn(name, secret_name), value)

    merged = {**(record.constants or {}), **req.constants}
    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_constants, name, record.service_name, merged, record, _constants_from_dict(merged))
    return {"status": "DEPLOYING"}


def _run_update_spec(name: str, record: AppRecord, new_tier: ResourceTier,
                     new_caller: bool, caller_flipping_on: bool) -> None:
    try:
        constants_list = _constants_from_dict(record.constants or {})
        spec = _build_spec(name, record.pg_database, new_tier, constants_list, new_caller)
        sf.alter_service_spec(record.service_name, spec)
        if caller_flipping_on:
            sf.set_caller_token_validity(record.service_name, 1800)
        if not _poll_status(record.service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {
            "resource_tier": str(new_tier.value) if hasattr(new_tier, "value") else str(new_tier),
            "use_caller_rights": new_caller,
            "last_deploy_status": "READY",
        })
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Spec update failed for %s", name)


@app.put("/apps/{name}/spec", status_code=202)
def update_spec(name: str, req: UpdateSpecRequest, background_tasks: BackgroundTasks,
                roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    if req.resource_tier is None and req.use_caller_rights is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of resource_tier or use_caller_rights must be provided",
        )

    new_tier = req.resource_tier if req.resource_tier is not None else ResourceTier(record.resource_tier)
    new_caller = req.use_caller_rights if req.use_caller_rights is not None else bool(record.use_caller_rights)
    caller_flipping_on = (not record.use_caller_rights) and new_caller

    registry.update_app(name, {"last_deploy_status": "DEPLOYING"})
    background_tasks.add_task(_run_update_spec, name, record, new_tier, new_caller, caller_flipping_on)
    return {"status": "DEPLOYING"}


@app.get("/activity")
def list_activity(app: Optional[str] = None, operator: Optional[str] = None, limit: int = 100,
                  roles: set[str] = Depends(caller_roles)):
    rows = activity.query(app=app, operator=operator, limit=limit)
    if roles & auth.PRIVILEGED_ROLES:
        return rows
    # Non-privileged operators see only activity for apps they own. Rows with no
    # app (e.g. create attempts) are visible only to privileged roles.
    visible = {a.name for a in registry.list_apps() if auth.authorize(a.owner_role, roles)}
    return [r for r in rows if r.get("app_name") in visible]


def _run_suspend(name: str, service_name: str) -> None:
    try:
        sf.suspend_service(service_name)
        if not _poll_status(service_name, "SUSPENDED", timeout_secs=120):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {"last_deploy_status": "SUSPENDED"})
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Suspend failed for %s", name)


def _run_resume(name: str, service_name: str) -> None:
    try:
        sf.resume_service(service_name)
        if not _poll_status(service_name, "RUNNING", timeout_secs=300):
            registry.update_app(name, {"last_deploy_status": "FAILED"})
            return
        registry.update_app(name, {"last_deploy_status": "READY"})
    except Exception:
        try:
            registry.update_app(name, {"last_deploy_status": "FAILED"})
        except Exception:
            pass
        logger.exception("Resume failed for %s", name)


@app.post("/apps/{name}/suspend", status_code=202)
def suspend_app(name: str, background_tasks: BackgroundTasks,
                roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    registry.update_app(name, {"last_deploy_status": "SUSPENDING"})
    background_tasks.add_task(_run_suspend, name, record.service_name)
    return {"status": "SUSPENDING"}


@app.post("/apps/{name}/resume", status_code=202)
def resume_app(name: str, background_tasks: BackgroundTasks,
               roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)
    registry.update_app(name, {"last_deploy_status": "RESUMING"})
    background_tasks.add_task(_run_resume, name, record.service_name)
    return {"status": "RESUMING"}


@app.delete("/apps/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(name: str, roles: set[str] = Depends(caller_roles)):
    record = _record_for_mutation(name, roles)

    try:
        sf.suspend_service(record.service_name)
        _poll_status(record.service_name, "SUSPENDED", timeout_secs=60)
    except Exception:
        pass

    sf.drop_service(record.service_name)
    # Dropping the service auto-drops its service roles (revoking the endpoint
    # grant from app_admin); the per-app application role persists, so drop it.
    sf.drop_app_access_role(name)
    registry.delete_app(name)
