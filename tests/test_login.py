"""Sign-in: captcha, browser-session cookie, idle timeout.  Run: python -m pytest tests"""
import sys
import types

sys.modules.setdefault("pymupdf", types.ModuleType("pymupdf"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, captcha, db, llm, main  # noqa: E402

PW = "Password123"


@pytest.fixture()
def admin(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "FILES_DIR", data / "files")
    monkeypatch.setattr(db, "DB_PATH", data / "contracts.db")
    monkeypatch.setattr(llm, "CONFIG_PATH", data / "config.json")
    with TestClient(main.app) as c:
        c.post("/api/auth/setup", json={"username": "admin", "password": PW, "display_name": "A",
                                        "employee_no": "E1"}).raise_for_status()
        yield c


def _captcha(client):
    r = client.get("/api/auth/captcha")
    assert r.status_code == 200 and r.json()["image"].startswith("data:image/png;base64,")
    cid = r.json()["id"]
    return cid, captcha._store[cid][0]


def _login(client, username="admin", password=PW, answer=None):
    cid, right = _captcha(client)
    return client.post("/api/auth/login", json={"username": username, "password": password,
                                                "captcha_id": cid, "captcha": right if answer is None else answer})


def test_login_needs_the_right_captcha(admin):
    c = TestClient(main.app)
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 400
    assert _login(c, answer="XXXX").status_code == 400
    r = _login(c, answer=None)
    assert r.status_code == 200
    # answer is case-insensitive
    cid, right = _captcha(c)
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW, "captcha_id": cid,
                                           "captcha": right.lower()}).status_code == 200


def test_captcha_works_only_once(admin):
    c = TestClient(main.app)
    cid, right = _captcha(c)
    body = {"username": "admin", "password": PW, "captcha_id": cid, "captcha": right}
    assert c.post("/api/auth/login", json=body).status_code == 200
    assert c.post("/api/auth/login", json=body).status_code == 400


def test_register_needs_captcha(admin):
    c = TestClient(main.app)
    body = {"username": "newbie", "password": PW, "password2": PW, "display_name": "N", "employee_no": "E9"}
    assert c.post("/api/auth/register", json=body).status_code == 400
    cid, right = _captcha(c)
    assert c.post("/api/auth/register", json=dict(body, captcha_id=cid, captcha=right)).status_code == 200


def test_cookie_is_forgotten_when_browser_closes(admin):
    r = _login(TestClient(main.app))
    cookie = r.headers["set-cookie"].lower()
    assert "max-age" not in cookie and "expires" not in cookie


def test_idle_login_expires(admin):
    c = TestClient(main.app)
    _login(c).raise_for_status()
    assert c.get("/api/auth/state").json()["user"]["username"] == "admin"
    db.ex("UPDATE sessions SET last_seen='2000-01-01 00:00:00'")  # nobody used it for a long time
    assert c.get("/api/auth/state").json()["user"] is None
    assert c.get("/api/contracts").status_code == 401


def test_login_never_lasts_longer_than_the_limit(admin):
    c = TestClient(main.app)
    _login(c).raise_for_status()
    db.ex("UPDATE sessions SET expires_at='2000-01-01 00:00:00'")
    assert c.get("/api/auth/state").json()["user"] is None
