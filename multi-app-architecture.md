# Multi-App Architecture on SPCS

This document tracks architecture decisions for deploying multiple Mendix applications on Snowpark Container Services.

## Goal

Simulate an architecture that supports 10+ Mendix apps running on shared Snowflake infrastructure, without requiring enterprise-scale resources for each app.

## Decisions

### 1. Shared Compute Pool

**Decision:** All apps share a single compute pool (`MENDIX_POC_POOL`, CPU_X64_S).

**Rationale:** Each Mendix app uses roughly 0.5-1 CPU and 1-2GB RAM when idle. A single node can host multiple lightweight services. For actual scale, increase `MAX_NODES` on the pool rather than creating separate pools per app.

**Trade-offs:**
- Pro: Cost-efficient, no idle pool overhead per app
- Pro: SPCS handles scheduling across the pool automatically
- Con: Noisy-neighbor risk under load (mitigated by resource requests/limits per service)
- Con: Single point of failure at pool level (acceptable for non-production)

---

### 2. Shared Postgres Instance, Separate Databases

**Decision:** All apps use the same `MENDIX_PG` instance (STANDARD_M, PostgreSQL 17), each with its own database.

**Rationale:** PostgreSQL natively supports multiple databases on a single instance. Mendix apps are configured with `RUNTIME_PARAMS_DATABASENAME` to target their specific database. This gives schema isolation without the cost of multiple PG instances.

**Trade-offs:**
- Pro: Single instance to manage, single egress IP set to whitelist
- Pro: No additional cost per app for PG hosting
- Con: Shared I/O and memory across databases (acceptable at this scale)
- Con: Requires psql access to create new databases (not in the deploy script today)

**Per-app setup:**
```sql
-- Connect to MENDIX_PG via psql, then:
CREATE DATABASE <app_name>;
GRANT ALL ON DATABASE <app_name> TO application;
```

---

### 3. Shared EAI (External Access Integration)

**Decision:** All services reuse `MENDIX_PG_EAI` for Postgres connectivity.

**Rationale:** The EAI whitelists SPCS egress IPs against the PG instance. Since all services run in the same compute pool (same egress IPs), a single EAI covers them all. The egress IP ranges are tied to the pool, not individual services.

**Current egress IPs:** 153.45.19.0/24, 153.45.95.0/24 (expires 2026-09-07)

---

### 4. Shared Image Registry, Separate Image Names

**Decision:** All apps push to the same image repository (`your_db/public/poc_repo`) with distinct image names per app.

**Rationale:** No need for separate repos. Image names provide sufficient isolation: `mendix-app:latest` (existing), `mendix-manufacturing:latest` (new), etc.

---

### 5. Separate File Storage Stages

**Decision:** Each app gets its own internal stage for file uploads.

**Rationale:** Mendix uses the filesystem for uploaded files, documents, and images. Separate stages prevent cross-app file access and simplify cleanup. Stages are cheap (just metadata).

**Naming convention:** `@YOUR_DB.PUBLIC.<APP_SHORT>_FILESTORAGE_STAGE`

---

### 6. Separate Services, Shared Schema

**Decision:** All services live in `YOUR_DB.PUBLIC` with distinct service names.

**Rationale:** Keeps everything in one place for simplicity. Each service gets its own endpoint URL (stable across ALTER SERVICE operations). Could move to per-app schemas for stricter isolation in production.

**Naming convention:** `<APP_NAME>_SERVICE` (e.g., `MANUFACTURING_SERVICE`)

---

### 7. Per-App Deploy Config

**Decision:** Each app has its own `deploy-config-<app>.json` in the Deploy Script folder.

**Rationale:** The deploy script accepts a `-Config` parameter. Separate configs keep credentials and resource sizing independent per app. The script is reusable without modification.

---

## Resource Sizing (Per App)

| Tier | CPU Request | CPU Limit | Memory Request | Memory Limit | Use Case |
|------|------------|-----------|----------------|--------------|----------|
| Small | 0.25 | 0.5 | 512M | 1G | Demo/empty apps |
| Medium | 0.5 | 1 | 1G | 2G | Apps with moderate data |
| Large | 1 | 2 | 2G | 4G | Apps with heavy processing |

---

## Current Deployments

| App | Service Name | PG Database | Image | Status |
|-----|-------------|-------------|-------|--------|
| SnowflakeNativeTest | MENDIX_SERVICE | postgres | mendix-app | Running |
| Manufacturing OT | MANUFACTURING_SERVICE | manufacturing | mendix-manufacturing | Planned |

---

## Open Questions

- **SSO for multiple apps:** The SnowflakeSSO module reads `Sf-Context-Current-User`. Does this work independently per service endpoint, or do tokens need per-service configuration?
- **Caller grants:** Are caller grants per-service or per-pool? If per-service, each new app needs its own GRANT CALLER statements.
- **PG connection pooling:** At 10+ apps, should we introduce PgBouncer or similar, or does Mendix's built-in pool suffice?
- **Auto-suspend:** Should each service have independent suspend/resume schedules, or one schedule for the whole pool?
