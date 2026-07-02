from __future__ import annotations


class TestActivityMiddleware:
    def test_mutation_records_one_row(self, client, fake_sf, fake_registry, fake_activity, make_record, role_headers):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE", operator="bob"))
        assert resp.status_code == 202
        assert len(fake_activity.rows) == 1
        row = fake_activity.rows[0]
        assert row["operator"] == "bob"
        assert row["action"] == "suspend"
        assert row["app_name"] == "myapp"
        assert row["result"] == "accepted"

    def test_rejected_mutation_records_rejected(self, client, fake_sf, fake_registry, fake_activity, role_headers):
        # stranger, no owned app -> 403 on suspend
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OTHER_ROLE", operator="eve"))
        assert resp.status_code == 404  # app doesn't exist at all
        assert len(fake_activity.rows) == 1
        assert fake_activity.rows[0]["result"] == "rejected (404)"

    def test_get_requests_record_nothing(self, client, fake_sf, fake_registry, fake_activity, role_headers):
        resp = client.get("/apps", headers=role_headers("PRIV_ROLE"))
        assert resp.status_code == 200
        assert fake_activity.rows == []

    def test_missing_operator_header_uses_anonymous(self, client, fake_sf, fake_registry, fake_activity, make_record):
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"
        # Internal headers present but no X-Operator: the middleware falls back to
        # auth.resolve_caller(request).user, which is None on this path (no
        # X-Operator sent), so the recorded operator is "<anonymous>".
        headers = {"X-Internal-Auth": "test-internal-token", "X-Operator-Roles": "OWNER_ROLE"}
        resp = client.post("/apps/myapp/suspend", headers=headers)
        assert resp.status_code == 202
        assert fake_activity.rows[0]["operator"] == "<anonymous>"

    def test_activity_insert_raising_does_not_break_response(self, client, fake_sf, fake_registry, fake_activity,
                                                              make_record, role_headers, monkeypatch):
        from app import activity
        record = make_record(name="myapp", owner_role="OWNER_ROLE")
        fake_registry.add(record)
        fake_sf.service_statuses[record.service_name] = "SUSPENDED"

        def raiser(**kwargs):
            raise RuntimeError("activity backend down")

        monkeypatch.setattr(activity, "insert", raiser)
        resp = client.post("/apps/myapp/suspend", headers=role_headers("OWNER_ROLE"))
        assert resp.status_code == 202
