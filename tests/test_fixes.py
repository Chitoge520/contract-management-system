"""Regression tests: Excel formula injection, department links of deleted contracts, backup temp files.

Run:  pip install pytest  &&  python -m pytest tests
OCR / PDF libraries are not needed: only .txt contracts are used and the LLM is replaced by a canned reply.
"""
import io
import json
import sys
import time
import types
import zipfile

import pytest

sys.modules.setdefault("pymupdf", types.ModuleType("pymupdf"))  # PDF reading is not exercised here

from fastapi.testclient import TestClient  # noqa: E402

from app import db, llm, main  # noqa: E402

PW = "Password123"
FAKE = {"contract_type": "service",
        "basic": {"contract_no": '=HYPERLINK("http://evil","click")', "title": "+cmd", "party_b": "@SUM(A1)"},
        "amount": {"currency": "USD", "total_amount": 100}, "line_items": []}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "FILES_DIR", data / "files")
    monkeypatch.setattr(db, "DB_PATH", data / "contracts.db")
    monkeypatch.setattr(llm, "CONFIG_PATH", data / "config.json")
    fake = tmp_path / "fake_llm.json"
    fake.write_text(json.dumps(FAKE), encoding="utf-8")
    monkeypatch.setenv("CMS_FAKE_LLM", str(fake))
    with TestClient(main.app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": PW, "display_name": "A",
                                            "employee_no": "E1"}).raise_for_status()
        fin = admin.post("/api/departments", json={"name": "Finance"}).json()["id"]
        legal = admin.post("/api/departments", json={"name": "Legal"}).json()["id"]
        admin.post("/api/users", json={"username": "bob", "password": PW, "display_name": "B", "employee_no": "E2",
                                       "role": "editor", "department_id": legal}).raise_for_status()
        yield {"admin": admin, "fin": fin, "legal": legal, "tmp": tmp_path}


def _upload(client, content=b"finance contract text", name="c.txt"):
    return client.post("/api/contracts", files=[("files", (name, content, "text/plain"))]).json()


def _wait_review(cid):
    for _ in range(100):
        if db.q("SELECT status FROM contracts WHERE id=?", (cid,), one=True)["status"] in ("review", "failed"):
            return
        time.sleep(0.05)
    raise AssertionError("extraction did not finish")


def test_export_does_not_create_formulas(env):
    from openpyxl import load_workbook
    cid = _upload(env["admin"])["created"][0]
    _wait_review(cid)
    wb = load_workbook(io.BytesIO(env["admin"].get("/api/export.xlsx").content))
    ws = wb.active
    for cell in ws[2]:
        assert cell.data_type != "f", f"{cell.coordinate} became a formula: {cell.value!r}"
    assert ws["B2"].value == "'" + FAKE["basic"]["contract_no"]
    assert ws["C2"].value == "'+cmd"


def test_deleted_contract_does_not_block_department_delete(env):
    admin = env["admin"]
    cid = _upload(admin)["created"][0]
    admin.put(f"/api/contracts/{cid}/departments", json={"department_ids": [env["fin"]]}).raise_for_status()
    _wait_review(cid)
    assert admin.delete(f"/api/contracts/{cid}").status_code == 200
    assert db.q("SELECT * FROM contract_departments WHERE contract_id=?", (cid,)) == []
    assert admin.delete(f"/api/departments/{env['fin']}").status_code == 200


def test_startup_removes_old_orphan_links(env):
    db.ex("INSERT INTO contract_departments (contract_id, department_id) VALUES (999, ?)", (env["fin"],))
    from app import access
    access.init()
    assert db.q("SELECT * FROM contract_departments WHERE contract_id=999") == []



def test_backup_includes_settings_and_cleans_up(env, monkeypatch):
    import tempfile
    admin = env["admin"]
    admin.put("/api/settings", json={"provider": "deepseek"}).raise_for_status()
    made = []
    real = tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        made.append(real(*a, **k))
        return made[-1]
    monkeypatch.setattr(main.tempfile, "mkdtemp", tracking_mkdtemp)
    r = admin.get("/api/backup")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "data/contracts.db" in names and "data/config.json" in names
    from pathlib import Path
    assert made and not Path(made[0]).exists(), "temporary backup folder was not removed"
