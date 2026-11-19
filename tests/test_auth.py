import time

import pytest
from fastapi.testclient import TestClient

from app import auth, db


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_MODE", "mock")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(auth, "SECRET_FILE", tmp_path / "session_secret")
    auth._failures.clear()

    def make(pin):
        if pin:
            monkeypatch.setenv("GATE_PIN", pin)
        else:
            monkeypatch.delenv("GATE_PIN", raising=False)
        from app.main import app
        return TestClient(app, follow_redirects=False)
    return make


def test_no_pin_means_open(make_client):
    with make_client(None) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/session").json() == {"pin_required": False}


def test_pin_required(make_client):
    with make_client("482913") as c:
        r = c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/api/visits").status_code == 401
        assert c.get("/static/index.html").status_code == 303
        assert c.get("/api/session").json() == {"pin_required": True}  # open, so clients can check
        assert "Wrong PIN" in c.post("/login", data={"pin": "000000"}).text

        r = c.post("/login", data={"pin": "482913"})
        assert r.status_code == 303 and auth.COOKIE in r.cookies
        assert c.get("/").status_code == 200
        assert c.get("/api/session").json() == {"pin_required": True}

        assert c.get("/logout").status_code == 303
        c.cookies.clear()
        assert c.get("/api/visits").status_code == 401


def test_lockout_after_repeated_wrong_pins(make_client):
    with make_client("482913") as c:
        for _ in range(auth.MAX_FAILURES):
            c.post("/login", data={"pin": "111111"})
        r = c.post("/login", data={"pin": "482913"})  # correct, but locked out
        assert "Too many wrong PINs" in r.text and auth.COOKIE not in r.cookies


def test_tokens_expire_and_follow_pin(make_client, monkeypatch):
    make_client("482913")
    token = auth.make_token()
    assert auth.valid_token(token)
    old = str(int(time.time()) - auth.SESSION_SECONDS - 1)
    assert not auth.valid_token(f"{old}.{auth._sign(old)}")
    monkeypatch.setenv("GATE_PIN", "999999")  # PIN changed -> existing sessions end
    assert not auth.valid_token(token)
    assert not auth.valid_token("garbage") and not auth.valid_token(None)
