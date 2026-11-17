import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, llm, pipeline

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED = {".pdf", ".docx", ".txt"}

app = FastAPI(title="Contract Management System")


@app.on_event("startup")
def _startup():
    db.init()
    pipeline.start()


def _get(cid):
    row = db.q("SELECT * FROM contracts WHERE id=?", (cid,), one=True)
    if not row:
        raise HTTPException(404, "Contract not found")
    return row


def _list_row(r):
    r = dict(r)
    r.pop("data_json", None)
    return r


# ---------------- contracts ----------------
@app.get("/api/contracts")
def list_contracts(q: str = "", type: str = "", party: str = "", status: str = "", lifecycle: str = "", year: str = ""):
    sql = "SELECT * FROM contracts WHERE 1=1"
    args = []
    if q.strip():
        for word in q.split():
            like = f"%{word.lower()}%"
            sql += (" AND (lower(coalesce(file_name,'')||' '||coalesce(contract_no,'')||' '||coalesce(title,'')||' '||"
                    "coalesce(title_zh,'')||' '||coalesce(party_a,'')||' '||coalesce(party_b,'')||' '||coalesce(notes,'')||' '||"
                    "coalesce(data_json,'')) LIKE ? OR id IN (SELECT contract_id FROM pages WHERE lower(text) LIKE ?))")
            args += [like, like]
    if type:
        sql += " AND contract_type=?"
        args.append(type)
    if party:
        sql += " AND lower(coalesce(party_b,'')||' '||coalesce(party_a,'')) LIKE ?"
        args.append(f"%{party.lower()}%")
    if status:
        sql += " AND status=?"
        args.append(status)
    if lifecycle:
        sql += " AND lifecycle_status=?"
        args.append(lifecycle)
    if year:
        sql += " AND (substr(coalesce(signing_date, effective_date, start_date, uploaded_at),1,4)=?)"
        args.append(year)
    sql += " ORDER BY id DESC"
    return [_list_row(r) for r in db.q(sql, args)]


@app.post("/api/contracts")
async def upload(files: List[UploadFile] = File(...)):
    created, duplicates, rejected = [], [], []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED:
            rejected.append(f.filename)
            continue
        content = await f.read()
        h = hashlib.sha256(content).hexdigest()
        dup = db.q("SELECT id, file_name FROM contracts WHERE file_hash=?", (h,), one=True)
        if dup:
            duplicates.append({"file": f.filename, "existing_id": dup["id"]})
            continue
        stored = f"{datetime.now():%Y%m%d%H%M%S}_{h[:10]}{ext}"
        (db.FILES_DIR / stored).write_bytes(content)
        cid = db.ex("INSERT INTO contracts (file_name, stored_name, file_hash, uploaded_at, updated_at, status) "
                    "VALUES (?,?,?,?,?, 'queued')", (f.filename, stored, h, db.now(), db.now()))
        pipeline.enqueue(cid, "read")
        created.append(cid)
    return {"created": created, "duplicates": duplicates, "rejected": rejected}


@app.get("/api/contracts/{cid}")
def get_contract(cid: int):
    row = _get(cid)
    data = json.loads(row["data_json"]) if row.get("data_json") else None
    row = _list_row(row)
    row["data"] = data
    return row


@app.put("/api/contracts/{cid}")
def update_contract(cid: int, body: dict = Body(...)):
    _get(cid)
    if "data" in body and body["data"] is not None:
        db.save_data(cid, body["data"])
    for k in ("lifecycle_status", "notes"):
        if k in body:
            db.ex(f"UPDATE contracts SET {k}=?, updated_at=? WHERE id=?", (body[k], db.now(), cid))
    return get_contract(cid)


@app.post("/api/contracts/{cid}/confirm")
def confirm(cid: int):
    row = _get(cid)
    if not row.get("data_json"):
        raise HTTPException(400, "Nothing to confirm")
    db.ex("UPDATE contracts SET status='confirmed', confirmed_at=?, updated_at=? WHERE id=?", (db.now(), db.now(), cid))
    return get_contract(cid)


@app.post("/api/contracts/{cid}/unconfirm")
def unconfirm(cid: int):
    _get(cid)
    db.ex("UPDATE contracts SET status='review', confirmed_at=NULL, updated_at=? WHERE id=?", (db.now(), cid))
    return get_contract(cid)


@app.post("/api/contracts/{cid}/reprocess")
def reprocess(cid: int, stage: str = "extract"):
    _get(cid)
    pipeline.enqueue(cid, "read" if stage == "read" else "extract")
    return {"ok": True}


@app.delete("/api/contracts/{cid}")
def delete(cid: int):
    row = _get(cid)
    db.delete_contract(cid)
    try:
        (db.FILES_DIR / row["stored_name"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/contracts/{cid}/file")
def get_file(cid: int, download: int = 0):
    row = _get(cid)
    path = db.FILES_DIR / row["stored_name"]
    media = "application/pdf" if path.suffix.lower() == ".pdf" else None
    if download:
        return FileResponse(path, media_type=media, filename=row["file_name"])
    return FileResponse(path, media_type=media,
                        headers={"Content-Disposition": "inline; filename*=UTF-8''contract" + path.suffix})


@app.get("/api/contracts/{cid}/pages")
def get_pages(cid: int):
    _get(cid)
    return db.q("SELECT page_no, text, redacted_text, is_ocr FROM pages WHERE contract_id=? ORDER BY page_no", (cid,))


# ---------------- line items / stats ----------------
@app.get("/api/items")
def items(q: str = "", type: str = "", party: str = "", confirmed_only: int = 0):
    sql = ("SELECT li.*, c.contract_no, c.title, c.title_zh, c.party_b, c.contract_type, c.status, c.signing_date "
           "FROM line_items li JOIN contracts c ON c.id = li.contract_id WHERE 1=1")
    args = []
    for word in q.split():
        sql += " AND lower(coalesce(li.name,'')||' '||coalesce(li.name_zh,'')||' '||coalesce(li.description,'')||' '||coalesce(li.category,'')) LIKE ?"
        args.append(f"%{word.lower()}%")
    if type:
        sql += " AND c.contract_type=?"
        args.append(type)
    if party:
        sql += " AND lower(coalesce(c.party_b,'')) LIKE ?"
        args.append(f"%{party.lower()}%")
    if confirmed_only:
        sql += " AND c.status='confirmed'"
    sql += " ORDER BY c.id DESC, li.seq"
    return db.q(sql, args)


@app.get("/api/stats")
def stats():
    by_status = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM contracts GROUP BY status")}
    totals = db.q("SELECT coalesce(currency,'?') currency, count(*) n, sum(total_amount) total FROM contracts "
                  "WHERE total_amount IS NOT NULL GROUP BY currency")
    parties = [r["party_b"] for r in db.q("SELECT DISTINCT party_b FROM contracts WHERE party_b IS NOT NULL ORDER BY party_b")]
    years = sorted({(r["y"] or "")[:4] for r in db.q(
        "SELECT coalesce(signing_date, effective_date, start_date, uploaded_at) y FROM contracts")} - {""}, reverse=True)
    return {"by_status": by_status, "totals": totals, "parties": parties, "years": years,
            "count": sum(by_status.values())}


# ---------------- export / backup ----------------
@app.get("/api/export.xlsx")
def export_xlsx(ids: str = ""):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    if ids:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        rows = [db.q("SELECT * FROM contracts WHERE id=?", (i,), one=True) for i in id_list]
        rows = [r for r in rows if r]
    else:
        rows = db.q("SELECT * FROM contracts ORDER BY id")
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F3A5F")

    def sheet(ws, headers, data, widths):
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in data:
            ws.append(r)
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "合同 Contracts"
    data = []
    for r in rows:
        d = json.loads(r["data_json"]) if r.get("data_json") else {}
        data.append([r["id"], r["contract_no"], r["title"], r["title_zh"], r["contract_type"], r["party_a"], r["party_b"],
                     r["signing_date"], r["effective_date"], r["start_date"], r["end_date"],
                     (d.get("basic") or {}).get("term_text"), r["currency"], r["total_amount"],
                     (d.get("amount") or {}).get("settlement_currency"), r["status"], r["lifecycle_status"],
                     d.get("summary_zh"), r["file_name"]])
    sheet(ws, ["ID", "合同号 No.", "名称 Title", "中文名称", "类型 Type", "甲方 Party A", "乙方 Party B", "签署日期 Signed",
               "生效日期 Effective", "开始 Start", "结束 End", "期限 Term", "币种 Currency", "总金额 Total",
               "结算币种 Settlement", "处理状态 Status", "合同状态 Lifecycle", "中文摘要 Summary", "文件 File"],
          data, [6, 18, 40, 30, 10, 30, 30, 12, 12, 12, 12, 30, 8, 14, 10, 10, 10, 60, 40])

    ws2 = wb.create_sheet("明细 Line items")
    data = []
    for r in rows:
        for it in db.q("SELECT * FROM line_items WHERE contract_id=? ORDER BY seq", (r["id"],)):
            data.append([r["id"], r["contract_no"], r["party_b"], it["seq"], it["category"], it["name"], it["name_zh"],
                         it["description"], it["quantity"], it["unit"], it["unit_price"], it["amount"], it["currency"],
                         it["page"]])
    sheet(ws2, ["合同ID", "合同号 No.", "乙方 Party B", "序号", "类别 Category", "名称 Item", "中文名称", "描述 Description",
                "数量 Qty", "单位 Unit", "单价 Unit price", "金额 Amount", "币种", "页码 Page"],
          data, [8, 18, 28, 6, 22, 30, 22, 50, 8, 8, 12, 14, 8, 8])

    ws3 = wb.create_sheet("付款 Payments")
    data = []
    for r in rows:
        d = json.loads(r["data_json"]) if r.get("data_json") else {}
        for p in d.get("payment_terms") or []:
            data.append([r["id"], r["contract_no"], r["party_b"], p.get("seq"), p.get("trigger"), p.get("trigger_zh"),
                         p.get("percentage"), db.to_float(p.get("amount")), r["currency"], p.get("due_rule"), p.get("page")])
    sheet(ws3, ["合同ID", "合同号 No.", "乙方 Party B", "期次", "付款条件 Trigger", "中文", "比例 %", "金额 Amount", "币种",
                "付款期限 Due", "页码"], data, [8, 18, 28, 6, 40, 30, 8, 14, 8, 40, 6])

    for w in (ws, ws2, ws3):
        for row in w.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"contracts_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/api/backup")
def backup():
    tmp = Path(tempfile.mkdtemp())
    snap = tmp / "contracts.db"
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    zpath = tmp / f"backup_{datetime.now():%Y%m%d_%H%M}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "data/contracts.db")
        for f in db.FILES_DIR.iterdir():
            z.write(f, f"data/files/{f.name}")
    return FileResponse(zpath, filename=zpath.name, media_type="application/zip")


# ---------------- settings ----------------
def _public_cfg(cfg):
    c = dict(cfg)
    key = c.get("api_key") or ""
    c["api_key_set"] = bool(key)
    c["api_key"] = (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("…" if key else "")
    return c


@app.get("/api/settings")
def get_settings():
    return {"config": _public_cfg(llm.load_config()), "providers": llm.PROVIDERS}


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    return {"config": _public_cfg(llm.save_config(body))}


@app.post("/api/settings/test")
def test_settings(body: dict = Body(default={})):
    cfg = llm.load_config()
    for k, v in (body or {}).items():
        if v not in (None, "", "__keep__"):
            cfg[k] = v
    try:
        out = llm.test_connection(cfg)
        return {"ok": True, "reply": out[:200]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:500]}, status_code=200)


# ---------------- static ----------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
