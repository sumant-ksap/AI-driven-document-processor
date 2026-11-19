"""Integration API (/api/v1) end to end, with the mock extractor (no Ollama needed)."""

import importlib
import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import auth, db, documents


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (1000, 800), "white").save(buf, "PNG")
    return ("bol.png", buf.getvalue(), "image/png")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_MODE", "mock")
    monkeypatch.delenv("GATE_PIN", raising=False)
    monkeypatch.delenv("SCAN_DOCS_DIR", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(auth, "SECRET_FILE", tmp_path / "session_secret")
    return monkeypatch


@pytest.fixture
def api(env):
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_ready(api, scan_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.get(f"/api/v1/scans/{scan_id}").json()
        if job["status"] != "processing":
            return job
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def scan_and_check_in(api, trailer="RDWY53001", **extra):
    job = wait_ready(api, api.post("/api/v1/scans", files=[("files", _png())]).json()["scan_id"])
    fields = {f["key"]: f["value"] for f in job["result"]["fields"]}
    # Distinct seal/tractor per trailer, or duplicate detection (correctly) blocks the second visit.
    fields.update(trailer_number=trailer, seal_number="SL-1", tractor_number=f"T-{trailer}", **extra)
    if trailer != "RDWY53001":
        fields["seal_number"] = f"SL-{trailer}"
    r = api.post("/api/v1/visits", json={"scan_id": job["scan_id"], "fields": fields,
                                         "line_items": [li["values"] for li in job["result"]["line_items"]],
                                         "reviewed_by": "tester"})
    assert r.status_code == 201, r.text
    return job, r.json()["visit_id"]


def test_fields_metadata(api):
    body = api.get("/api/v1/fields").json()
    keys = {f["key"]: f for f in body["fields"]}
    assert keys["trailer_number"]["required"] and keys["bol_number"]["recommended"]
    assert keys["ship_date"]["format"]["pattern"]
    assert "marks" in body["line_item_columns"]


def test_background_scan_poll_and_documents(api):
    r = api.post("/api/v1/scans", files=[("files", _png())])
    assert r.status_code == 202 and r.headers["location"] == f"/api/v1/scans/{r.json()['scan_id']}"
    assert r.json()["status"] in ("processing", "ready")
    job = wait_ready(api, r.json()["scan_id"])
    assert job["status"] == "ready" and job["result"]["extractor"] == "mock"
    url = job["files"][0]["download_url"]
    doc = api.get(url)
    assert doc.status_code == 200 and doc.content == _png()[1] and doc.headers["content-type"] == "image/png"


def test_wait_true_returns_result(api):
    r = api.post("/api/v1/scans?wait=true", files=[("files", _png())])
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["result"]["fields"]


def test_scan_survives_restart(env):
    from app.main import app
    with TestClient(app) as c:
        scan_id = c.post("/api/v1/scans?wait=true", files=[("files", _png())]).json()["scan_id"]
    with TestClient(app) as c:  # new lifespan = server restart; nothing kept in memory
        assert c.get(f"/api/v1/scans/{scan_id}").json()["status"] == "ready"


def test_failed_scan_reported(api, monkeypatch):
    from app.extractor import ExtractionError, MockExtractor

    def boom(self, *a):
        raise ExtractionError("Could not reach Ollama")
    monkeypatch.setattr(MockExtractor, "extract", boom)
    job = wait_ready(api, api.post("/api/v1/scans", files=[("files", _png())]).json()["scan_id"])
    assert job["status"] == "failed" and "Ollama" in job["error"] and job["result"] is None
    r = api.post("/api/v1/visits", json={"scan_id": job["scan_id"], "fields": {}, "reviewed_by": "x"})
    assert r.status_code == 409


def test_documents_not_stored_when_disabled(api, env):
    env.setenv("STORE_DOCUMENTS", "false")
    job = wait_ready(api, api.post("/api/v1/scans", files=[("files", _png())]).json()["scan_id"])
    assert job["files"][0]["download_url"] is None
    assert api.get(f"/api/v1/scans/{job['scan_id']}/documents/0").status_code == 404


def test_revalidate_check_in_and_visit_details(api):
    job, visit_id = scan_and_check_in(api)
    scan = api.get(f"/api/v1/scans/{job['scan_id']}").json()
    assert scan["status"] == "checked_in" and scan["visit_id"] == visit_id
    v = api.get(f"/api/v1/visits/{visit_id}").json()
    assert v["fields"]["trailer_number"] == "RDWY53001" and v["scan_id"] == job["scan_id"]
    assert v["line_items"] and v["corrections"]["seal_number"]["final"] == "SL-1"
    # Second check-in of the same scan is refused
    again = api.post("/api/v1/visits", json={"scan_id": job["scan_id"], "fields": v["fields"], "reviewed_by": "x"})
    assert again.status_code == 409 and "already checked in" in again.json()["detail"]


def test_duplicate_blocks_until_override(api):
    scan_and_check_in(api)
    job = wait_ready(api, api.post("/api/v1/scans", files=[("files", _png())]).json()["scan_id"])
    fields = {f["key"]: f["value"] for f in job["result"]["fields"]} | {"trailer_number": "RDWY53001"}
    flags = api.post(f"/api/v1/scans/{job['scan_id']}/revalidate", json={"fields": fields}).json()["flags"]
    assert any(f["code"] == "possible_duplicate" for f in flags)
    body = {"scan_id": job["scan_id"], "fields": fields, "reviewed_by": "tester"}
    assert api.post("/api/v1/visits", json=body).status_code == 409
    assert api.post("/api/v1/visits", json=body | {"override_reason": "confirmed second load"}).status_code == 201


def test_list_filter_page_edit_delete_checkout_export(api):
    _, v1 = scan_and_check_in(api, trailer="TRL0001")
    _, v2 = scan_and_check_in(api, trailer="TRL0002", bol_number="BOL-2")
    page = api.get("/api/v1/visits?limit=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["items"][0]["id"] == v2
    assert [v["id"] for v in api.get("/api/v1/visits?trailer_number=trl0001").json()["items"]] == [v1]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert api.get(f"/api/v1/visits?from={today}&to={today}").json()["total"] == 2
    assert api.get("/api/v1/visits?from=2000-01-01&to=2000-01-02").json()["total"] == 0

    # Edit: partial update recorded in the audit trail
    r = api.patch(f"/api/v1/visits/{v1}", json={"fields": {"seal_number": "SL-9"}, "reviewed_by": "supervisor",
                                                 "reason": "seal re-read at dock"})
    assert r.status_code == 200, r.text
    v = r.json()["visit"]
    assert v["fields"]["seal_number"] == "SL-9" and v["seal_number"] == "SL-9"
    assert v["corrections"]["_edits"][0]["changes"]["seal_number"] == {"from": "SL-TRL0001", "to": "SL-9"}
    assert not any(f["code"] == "possible_duplicate" and f"#{v1} " in f["message"] for f in r.json()["flags"])

    # Check-out, then a second check-out is refused
    assert api.post(f"/api/v1/visits/{v1}/check-out").json()["status"] == "checked_out"
    assert api.post(f"/api/v1/visits/{v1}/check-out").status_code == 409

    # Export: header + 2 rows, field columns present
    csv_text = api.get("/api/v1/visits/export.csv").content.decode("utf-8-sig")
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("visit_id,created_at") and "trailer_number" in lines[0] and len(lines) == 3

    # Soft delete: hidden by default, kept for audit
    assert api.delete(f"/api/v1/visits/{v2}?reviewed_by=supervisor&reason=test entry").status_code == 204
    assert [v["id"] for v in api.get("/api/v1/visits").json()["items"]] == [v1]
    assert api.get("/api/v1/visits?status=deleted").json()["items"][0]["id"] == v2
    assert api.get(f"/api/v1/visits/{v2}").json()["corrections"]["_edits"][-1]["changes"]["status"]["to"] == "deleted"
    assert api.patch(f"/api/v1/visits/{v2}", json={"fields": {}, "reviewed_by": "x"}).status_code == 409
    assert api.get("/api/v1/visits/999").status_code == 404


def test_master_data_crud(api):
    r = api.post("/api/v1/carriers", json=[{"scac": "ACME", "name": "Acme Haulage"},
                                           {"scac": "RDWY", "name": "Roadway Freight", "active": False}])
    assert r.status_code == 200 and len(r.json()["items"]) == 2
    carriers = {c["scac"]: c for c in api.get("/api/v1/carriers").json()["items"]}
    assert carriers["ACME"]["name"] == "Acme Haulage" and carriers["RDWY"]["active"] == 0
    assert api.post("/api/v1/carriers", json={"scac": "toolong", "name": "x"}).status_code == 422

    d = api.post("/api/v1/drivers", json={"name": "Pat Lee", "license_number": "P123"}).json()["items"][0]
    assert d["id"] and api.post("/api/v1/drivers", json={"id": d["id"], "name": "Pat Lee Jr"}).json()["items"][0]["name"] == "Pat Lee Jr"
    a = api.post("/api/v1/appointments", json={"bol_number": "BOL-NEW", "expected_date": "2026-12-01"}).json()["items"][0]
    assert a["status"] == "scheduled" and a["expected_date"] == "2026-12-01"
    assert api.post("/api/v1/trailers", json={"trailer_number": "ACME001", "carrier_scac": "ACME"}).status_code == 200

    assert api.delete("/api/v1/trailers/ACME001").status_code == 204
    assert api.delete("/api/v1/trailers/ACME001").status_code == 404
    assert api.delete(f"/api/v1/appointments/{a['id']}").status_code == 204


def test_new_master_data_used_by_validation(api):
    api.post("/api/v1/carriers", json={"scac": "ZZTR", "name": "Zed Trucking"})
    job = wait_ready(api, api.post("/api/v1/scans", files=[("files", _png())]).json()["scan_id"])
    fields = {f["key"]: f["value"] for f in job["result"]["fields"]} | {"carrier_scac": "ZZTR", "carrier_name": "Zed Trucking"}
    flags = api.post(f"/api/v1/scans/{job['scan_id']}/revalidate", json={"fields": fields}).json()["flags"]
    assert not any(f["code"] == "unknown_carrier" for f in flags)


def test_api_keys(env, tmp_path):
    env.setenv("GATE_PIN", "482913")
    with db.connect() as conn:
        key_id, key = db.create_api_key(conn, "YMS")
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/visits").status_code == 401
        assert c.get("/api/v1/visits", headers={"X-API-Key": "gsk_wrong"}).status_code == 401
        assert c.get("/api/v1/visits", headers={"X-API-Key": key}).status_code == 200
        assert c.get("/api/v1/visits", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        r = c.post("/api/v1/scans?wait=true", files=[("files", _png())], headers={"X-API-Key": key})
        assert r.status_code == 200
        with db.connect() as conn:
            assert db.list_api_keys(conn)[0]["last_used_at"]
            assert db.revoke_api_key(conn, key_id)
            assert conn.execute("SELECT COUNT(*) FROM api_keys WHERE key_hash = ?", (key,)).fetchone()[0] == 0  # not stored in clear
        assert c.get("/api/v1/visits", headers={"X-API-Key": key}).status_code == 401


def test_cors(env):
    import app.main as main_module
    env.setenv("CORS_ORIGINS", "https://yms.example.com")
    try:
        main_module = importlib.reload(main_module)
        with TestClient(main_module.app) as c:
            pre = c.options("/api/v1/visits", headers={"Origin": "https://yms.example.com",
                                                      "Access-Control-Request-Method": "GET",
                                                      "Access-Control-Request-Headers": "x-api-key"})
            assert pre.status_code == 200
            assert pre.headers["access-control-allow-origin"] == "https://yms.example.com"
            other = c.get("/api/v1/fields", headers={"Origin": "https://evil.example.com"})
            assert "access-control-allow-origin" not in other.headers
    finally:
        env.delenv("CORS_ORIGINS")
        importlib.reload(main_module)


def test_documents_cleanup(env, tmp_path):
    old = documents.docs_dir() / "aaaaaaaaaaaa"
    old.mkdir(parents=True)
    (old / "0_bol.png").write_bytes(b"x")
    import os
    os.utime(old, (time.time() - 100 * 86400,) * 2)
    fresh = documents.docs_dir() / "bbbbbbbbbbbb"
    fresh.mkdir()
    assert documents.cleanup() == 1 and not old.exists() and fresh.exists()
