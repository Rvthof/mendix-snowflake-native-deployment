# Mendix on SPCS: Caveats and Future Ideas

## Caveats

### No custom domain for SPCS endpoints

SPCS assigns a URL of the form `<hash>-<org>-<account>.snowflakecomputing.app` at service creation time. There is no support for custom domains, CNAME mapping, or vanity URLs.

**Impact:** If the service is dropped and recreated, the URL changes. Bookmarks, integrations, and any hardcoded references break.

**Mitigation:** Always use `ALTER SERVICE ... FROM SPECIFICATION` to update the service in-place. Only `DROP SERVICE` + `CREATE SERVICE` generates a new URL.

**Workaround for production:** Place a reverse proxy (Cloudflare, nginx, AWS ALB) in front of the SPCS endpoint with a CNAME on your own domain.

---

### Stage volume performance not yet benchmarked

The Mendix file storage is backed by a Snowflake stage volume. Stage volumes are optimized for large sequential reads/writes, not random I/O.

**Impact:** Large file uploads or high-frequency small file operations may be slower than local disk or S3.

**Action needed:** Benchmark with realistic file sizes and concurrency. If performance is insufficient, add a `block` volume for temp/random-IO workloads alongside the stage volume for documents.

---

### Trial license time limit

Without a production license, the Mendix runtime terminates after ~2 hours. SPCS auto-restarts it, but sessions and in-memory state are lost.

**Mitigation:** Add license env vars (`RUNTIME_LICENSE_ID`, `RUNTIME_LICENSE_KEY`). License validation may require egress to `licensing.mendix.com:443` via an EAI.

---

### SPCS egress IP ranges have an expiry date

The egress IPs used to whitelist SPCS on the Snowflake Postgres network policy (`SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()`) have an `expires` field. Current expiry: **2026-09-07**.

**Impact:** If IPs rotate and the network rule isn't updated, the Mendix app loses database connectivity.

**Action needed:** Monitor before expiry. Re-run `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` and update the `SPCS_TO_PG_INGRESS` network rule with new CIDRs.

---

### Caller's rights token expiry UX

When a caller token expires mid-session (user idle for >30 minutes without the refresh snippet triggering, or browser tab backgrounded), Snowflake queries throw a `RuntimeException`. The user sees an error page and must log out/in to restore the token.

**Current status:** The `Snippet_TriggerSFTokenRefresh` keepalive mitigates this for active users. Background tabs may still lose their token if the browser suspends JS timers.

**Future improvement:** Catch the auth exception in the query microflow and redirect to `/headersso/` for silent re-authentication instead of showing an error.

---

### JDBC connection pooling per user not implemented

Each call to `GetCompoundToken` + `ExecuteQuery` creates a new JDBC connection (via HikariCP). There is no connection reuse across requests for the same user.

**Impact:** Acceptable for low-concurrency POC/demo use. May become a bottleneck with many concurrent users.

**Future improvement:** Investigate user-keyed connection pooling or session-scoped connection caching in the External Database Connector.

---

## Future Ideas

### Multi-instance deployment for HA

**What:** Run `MIN_INSTANCES = 2` for high availability and load balancing.

**Prerequisite:** Already using Snowflake Postgres (shared DB across instances). Done.

**Mendix consideration:** Set `com.mendix.core.isClusterSlave = true` on non-leader instances, or use Mendix's built-in cluster leader election (requires shared DB).

---

### External Access Integration for Mendix egress

**What:** Allow the Mendix container to reach external services beyond the Postgres database.

**Potential needs:**
- `licensing.mendix.com:443` for license validation
- External REST/SOAP APIs consumed by the Mendix app
- SSO/SAML identity providers
- Email (SMTP) servers

**How:** Create network rules for each host:port, bundle into an EAI, attach with `ALTER SERVICE ... SET EXTERNAL_ACCESS_INTEGRATIONS = (...)`.

---

### Private Link for Snowflake Postgres (Business Critical upgrade)

**What:** Replace the current IP-whitelist approach with Private Link for the Postgres connection. Traffic stays on Snowflake's internal backbone instead of traversing the public internet.

**When:** If/when the account is upgraded to Business Critical edition.

**How:** `ALTER POSTGRES INSTANCE ENABLE PRIVATELINK`, provision SPCS endpoint via `SYSTEM$PROVISION_PRIVATELINK_ENDPOINT`, use `PRIVATE_HOST_PORT` network rule type.

---

### Service identity JDBC connection

**What:** Use the SPCS service token (auto-injected at `/snowflake/session/token`) to query Snowflake tables from within Mendix via JDBC. Simpler than caller's rights; runs as the service owner role.

**Use case:** Background jobs, scheduled data syncs, or admin dashboards that don't need per-user access control.

**Status:** Not yet implemented. The caller's rights approach (compound token) is the primary path. Service identity could be added as a simpler alternative for scenarios where per-user access isn't needed.

**How:** Read `/snowflake/session/token`, connect via JDBC with `host=$SNOWFLAKE_HOST`, `authenticator=oauth`, `token=<service-token>`. No caller token needed; no user context required.
