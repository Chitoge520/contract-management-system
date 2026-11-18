"""Contract list: year column, year filter, paging, export of all pages; password change signs out.
Run: python -m pytest tests"""
import io
import sys
import types

sys.modules.setdefault("pymupdf", types.ModuleType("pymupdf"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import captcha, db, llm, main  # noqa: E402

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
        # 30 contracts in 2024, 15 in 2025 (by signing date), 5 without dates uploaded in 2026
        for i in range(50):
            signed = "2024-03-01" if i < 30 else "2025-06-01" if i < 45 else None
            db.ex("INSERT INTO contracts (file_name, stored_name, uploaded_at, status, signing_date, contract_no) "
                  "VALUES (?,?,?,?,?,?)", (f"c{i}.txt", f"c{i}.txt", "2026-01-05 10:00:00", "review", signed, f"NO-{i}"))
        yield c


def test_first_page_and_total(admin):
    r = admin.get("/api/contracts?page=1&page_size=20").json()
    assert r["total"] == 50 and len(r["rows"]) == 20
    assert [x["year"] for x in r["rows"]][:6] == ["2026"] * 5 + ["2025"]  # newest year first
    last = admin.get("/api/contracts?page=3&page_size=20").json()
    assert len(last["rows"]) == 10 and last["rows"][-1]["year"] == "2024"


def test_year_filter(admin):
    r = admin.get("/api/contracts?year=2025&page=1&page_size=20").json()
    assert r["total"] == 15 and {x["year"] for x in r["rows"]} == {"2025"}
    # the contracts without dates are filed under their upload year
    assert admin.get("/api/contracts?year=2026&page=1&page_size=20").json()["total"] == 5


def test_no_year_means_all_years(admin):
    assert admin.get("/api/contracts?year=&page=1&page_size=100").json()["total"] == 50


def test_whole_list_without_paging_still_works(admin):
    r = admin.get("/api/contracts").json()  # used by the AI assistant's contract picker
    assert isinstance(r, list) and len(r) == 50


def test_bad_department_filter_is_400(admin):
    assert admin.get("/api/contracts?dept=abc").status_code == 400


def test_export_takes_every_page_of_the_filter(admin):
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(admin.get("/api/export.xlsx?year=2024").content)).active
    assert ws.max_row - 1 == 30
    ws = load_workbook(io.BytesIO(admin.get("/api/export.xlsx").content)).active
    assert ws.max_row - 1 == 50


def test_password_change_signs_out_everywhere(admin):
    other = TestClient(main.app)  # the same user signed in on a second computer
    cid = other.get("/api/auth/captcha").json()["id"]
    other.post("/api/auth/login", json={"username": "admin", "password": PW, "captcha_id": cid,
                                        "captcha": captcha._store[cid][0]}).raise_for_status()
    r = admin.post("/api/auth/password", json={"old_password": PW, "new_password": "NewPassword456"})
    assert r.status_code == 200 and r.json()["relogin"] is True
    assert admin.get("/api/contracts").status_code == 401
    assert other.get("/api/contracts").status_code == 401
