import hashlib
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, llm, pipeline
from .auth import current_user, require

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED = {".pdf", ".docx", ".txt"}

app = FastAPI(title="Contract Management System")
editor = require("editor")
admin = require("admin")


@app.on_event("startup")
def _startup():
    db.init()
    auth.init()
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


def _name(user):
    return user.get("display_name") or user.get("username")


# ================= auth =================
def _set_cookie(resp: Response, token: str):
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax")


@app.get("/api/auth/state")
def auth_state(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        user = None
    return {"needs_setup": auth.user_count() == 0, "user": auth.public_user(user)}


@app.post("/api/auth/setup")
def auth_setup(response: Response, body: dict = Body(...)):
    if auth.user_count() > 0:
        raise HTTPException(400, "Already set up")
    u = auth.create_user(body.get("username"), body.get("password"), "admin", body.get("display_name"))
    auth.log(u, "setup")
    _set_cookie(response, auth.new_session(u["id"]))
    return {"user": auth.public_user(u)}


@app.post("/api/auth/login")
def login(response: Response, body: dict = Body(...)):
    u = db.q("SELECT * FROM users WHERE lower(username)=lower(?)", ((body.get("username") or "").strip(),), one=True)
    if not u or not u["active"] or not auth.check_password(body.get("password") or "", u["password_hash"]):
        raise HTTPException(401, "用户名或密码错误 / Wrong username or password")
    db.ex("UPDATE users SET last_login=? WHERE id=?", (db.now(), u["id"]))
    auth.log(u, "login")
    _set_cookie(response, auth.new_session(u["id"]))
    return {"user": auth.public_user(u)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.end_session(token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: dict = Body(...), user=Depends(current_user)):
    if not auth.check_password(body.get("old_password") or "", user["password_hash"]):
        raise HTTPException(400, "原密码不正确 / Current password is wrong")
    auth.validate_password(body.get("new_password"))
    db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body["new_password"]), user["id"]))
    auth.log(user, "password_change")
    return {"ok": True}


# ================= users (admin) =================
@app.get("/api/users")
def list_users(user=Depends(admin)):
    return [auth.public_user(u) for u in db.q("SELECT * FROM users ORDER BY id")]


@app.post("/api/users")
def add_user(body: dict = Body(...), user=Depends(admin)):
    u = auth.create_user(body.get("username"), body.get("password"), body.get("role") or "viewer", body.get("display_name"))
    auth.log(user, "user_create", detail={"username": u["username"], "role": u["role"]})
    return auth.public_user(u)


@app.put("/api/users/{uid}")
def update_user(uid: int, body: dict = Body(...), user=Depends(admin)):
    u = db.q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        raise HTTPException(404, "User not found")
    changes = {}
    if "role" in body and body["role"] != u["role"]:
        if body["role"] not in auth.ROLES:
            raise HTTPException(400, "Invalid role")
        changes["role"] = [u["role"], body["role"]]
    if "active" in body and int(bool(body["active"])) != u["active"]:
        changes["active"] = [u["active"], int(bool(body["active"]))]
    if "display_name" in body and (body["display_name"] or "") != (u["display_name"] or ""):
        changes["display_name"] = [u["display_name"], body["display_name"]]
    # never lock out the last active admin
    becomes_non_admin = changes.get("role", [None, "admin"])[1] != "admin" or changes.get("active", [1, 1])[1] == 0
    if u["role"] == "admin" and becomes_non_admin:
        n = db.q("SELECT count(*) n FROM users WHERE role='admin' AND active=1 AND id<>?", (uid,), one=True)["n"]
        if n == 0:
            raise HTTPException(400, "至少需要保留一个可用的管理员 / At least one active admin is required")
    for k, (_, v) in changes.items():
        db.ex(f"UPDATE users SET {k}=? WHERE id=?", (v, uid))
    if body.get("password"):
        auth.validate_password(body["password"])
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body["password"]), uid))
        db.ex("DELETE FROM sessions WHERE user_id=?", (uid,))
        changes["password"] = "reset"
    if changes.get("active", [1, 1])[1] == 0:
        db.ex("DELETE FROM sessions WHERE user_id=?", (uid,))
    if changes:
        auth.log(user, "user_update", detail={"username": u["username"], "changes": changes})
    return auth.public_user(db.q("SELECT * FROM users WHERE id=?", (uid,), one=True))


# ================= audit =================
@app.get("/api/audit")
def audit(user_q: str = "", action: str = "", contract_id: int = 0, date_from: str = "", date_to: str = "",
          limit: int = 200, offset: int = 0, user=Depends(current_user)):
    sql, args = "FROM audit_log WHERE 1=1", []
    if user_q:
        sql += " AND lower(coalesce(username,'')) LIKE ?"
        args.append(f"%{user_q.lower()}%")
    if action:
        sql += " AND action=?"
        args.append(action)
    if contract_id:
        sql += " AND contract_id=?"
        args.append(contract_id)
    if date_from:
        sql += " AND ts>=?"
        args.append(date_from)
    if date_to:
        sql += " AND ts<=?"
        args.append(date_to + " 23:59:59")
    total = db.q("SELECT count(*) n " + sql, args, one=True)["n"]
    rows = db.q("SELECT * " + sql + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [min(limit, 1000), offset])
    _decorate(rows)
    return {"total": total, "rows": rows}


def _decorate(rows):
    """Decode details and show each contract by its current number/title (it may not have had one at upload time)."""
    current = {}
    for r in rows:
        r["detail"] = json.loads(r["detail"]) if r["detail"] else None
        cid = r.get("contract_id")
        if cid and cid not in current:
            current[cid] = auth.contract_label(cid)
        if cid and current.get(cid):
            r["contract_label"] = current[cid]


@app.get("/api/contracts/{cid}/history")
def history(cid: int, user=Depends(current_user)):
    rows = db.q("SELECT * FROM audit_log WHERE contract_id=? ORDER BY id DESC", (cid,))
    _decorate(rows)
    return rows


# ================= contracts =================
def _snippet(text, word, width=110):
    i = text.lower().find(word.lower())
    if i < 0:
        return None
    s, e = max(0, i - width), min(len(text), i + len(word) + width)
    return ("…" if s else "") + re.sub(r"\s+", " ", text[s:e]).strip() + ("…" if e < len(text) else "")


def _hits(cid, data, words):
    """What matched inside one contract, grouped for the search result view."""
    lw = [w.lower() for w in words]

    def match(*parts):
        text = " ".join(str(p) for p in parts if p not in (None, "")).lower()
        return all(w in text for w in lw)

    hits = {"items": [], "terms": [], "pages": [], "page_total": 0}
    for i, it in enumerate(data.get("line_items") or []):
        if match(it.get("name"), it.get("name_zh"), it.get("description"), it.get("category")):
            hits["items"].append({"index": i, "name": it.get("name"), "name_zh": it.get("name_zh"),
                                  "description": it.get("description"), "amount": it.get("amount"),
                                  "currency": it.get("currency"), "page": it.get("page")})
    groups = [("payments", "payment_terms", ("trigger", "trigger_zh", "due_rule", "conditions"), ("trigger_zh", "trigger")),
              ("payments", "milestones", ("name", "name_zh", "deadline_rule", "deliverables"), ("name_zh", "name")),
              ("terms", "penalties", ("type", "type_zh", "rule", "cap"), ("type_zh", "type")),
              ("terms", "key_clauses", ("topic", "topic_zh", "summary_en", "summary_zh"), ("topic_zh", "topic")),
              ("overview", "type_specific", ("label", "label_zh", "value"), ("label_zh", "label"))]
    for tab, key, fields, title_keys in groups:
        for it in data.get(key) or []:
            if match(*(it.get(f) for f in fields)):
                title = next((it.get(k) for k in title_keys if it.get(k)), key)
                body = " / ".join(str(it.get(f)) for f in fields if it.get(f) and f not in title_keys)
                hits["terms"].append({"tab": tab, "kind": key, "title": title, "text": body[:220], "page": it.get("page")})
    for key in ("summary_zh", "summary_en", "scope_zh", "scope_en"):
        if data.get(key) and match(data.get(key)):
            hits["terms"].append({"tab": "overview", "kind": key, "title": key, "text": _snippet(data[key], words[0]) or "", "page": None})
            break
    for p in db.q("SELECT page_no, text FROM pages WHERE contract_id=? ORDER BY page_no", (cid,)):
        if match(p["text"]):
            hits["page_total"] += 1
            if len(hits["pages"]) < 3:
                hits["pages"].append({"page": p["page_no"], "text": _snippet(p["text"], words[0])})
    return hits


@app.get("/api/contracts")
def list_contracts(q: str = "", type: str = "", party: str = "", status: str = "", lifecycle: str = "", year: str = "",
                   user=Depends(current_user)):
    sql = "SELECT * FROM contracts WHERE 1=1"
    args = []
    words = q.split()
    for word in words:
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
    out = []
    for r in db.q(sql, args):
        row = _list_row(r)
        if words:
            row["hits"] = _hits(r["id"], json.loads(r["data_json"]) if r.get("data_json") else {}, words)
        out.append(row)
    return out


@app.post("/api/contracts")
async def upload(files: List[UploadFile] = File(...), user=Depends(editor)):
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
        cid = db.ex("INSERT INTO contracts (file_name, stored_name, file_hash, uploaded_at, updated_at, status, uploaded_by) "
                    "VALUES (?,?,?,?,?, 'queued', ?)", (f.filename, stored, h, db.now(), db.now(), _name(user)))
        auth.log(user, "upload", cid, {"file": f.filename}, label=f.filename)
        pipeline.enqueue(cid, "read")
        created.append(cid)
    return {"created": created, "duplicates": duplicates, "rejected": rejected}


@app.get("/api/contracts/{cid}")
def get_contract(cid: int, user=Depends(current_user)):
    row = _get(cid)
    data = json.loads(row["data_json"]) if row.get("data_json") else None
    row = _list_row(row)
    row["data"] = data
    return row


@app.put("/api/contracts/{cid}")
def update_contract(cid: int, body: dict = Body(...), user=Depends(editor)):
    row = _get(cid)
    if "data" in body and body["data"] is not None:
        old = json.loads(row["data_json"]) if row.get("data_json") else {}
        changes = auth.diff(old, body["data"])
        if changes:
            db.save_data(cid, body["data"])
            db.ex("UPDATE contracts SET updated_by=? WHERE id=?", (_name(user), cid))
            reopened = row["status"] == "confirmed"
            if reopened:  # a confirmed contract that is edited must be reviewed again
                db.ex("UPDATE contracts SET status='review', confirmed_by=NULL, confirmed_at=NULL WHERE id=?", (cid,))
            auth.log(user, "edit", cid, {"changes": changes, "reopened": reopened})
    for k in ("lifecycle_status", "notes"):
        if k in body and (body[k] or "") != (row.get(k) or ""):
            db.ex(f"UPDATE contracts SET {k}=?, updated_at=?, updated_by=? WHERE id=?", (body[k], db.now(), _name(user), cid))
            auth.log(user, k, cid, {"old": row.get(k), "new": body[k]})
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/confirm")
def confirm(cid: int, user=Depends(editor)):
    row = _get(cid)
    if not row.get("data_json"):
        raise HTTPException(400, "Nothing to confirm")
    db.ex("UPDATE contracts SET status='confirmed', confirmed_at=?, confirmed_by=?, updated_at=? WHERE id=?",
          (db.now(), _name(user), db.now(), cid))
    auth.log(user, "confirm", cid)
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/unconfirm")
def unconfirm(cid: int, user=Depends(editor)):
    _get(cid)
    db.ex("UPDATE contracts SET status='review', confirmed_at=NULL, confirmed_by=NULL, updated_at=? WHERE id=?", (db.now(), cid))
    auth.log(user, "unconfirm", cid)
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/reprocess")
def reprocess(cid: int, stage: str = "extract", user=Depends(editor)):
    _get(cid)
    db.ex("UPDATE contracts SET confirmed_at=NULL, confirmed_by=NULL WHERE id=?", (cid,))
    auth.log(user, "reread" if stage == "read" else "reextract", cid)
    pipeline.enqueue(cid, "read" if stage == "read" else "extract")
    return {"ok": True}


@app.delete("/api/contracts/{cid}")
def delete(cid: int, user=Depends(admin)):
    row = _get(cid)
    auth.log(user, "delete", cid, {"file": row["file_name"]})
    db.delete_contract(cid)
    try:
        (db.FILES_DIR / row["stored_name"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/contracts/{cid}/file")
def get_file(cid: int, download: int = 0, user=Depends(current_user)):
    row = _get(cid)
    path = db.FILES_DIR / row["stored_name"]
    media = "application/pdf" if path.suffix.lower() == ".pdf" else None
    if download:
        return FileResponse(path, media_type=media, filename=row["file_name"])
    return FileResponse(path, media_type=media,
                        headers={"Content-Disposition": "inline; filename*=UTF-8''contract" + path.suffix})


@app.get("/api/contracts/{cid}/pages")
def get_pages(cid: int, user=Depends(current_user)):
    _get(cid)
    return db.q("SELECT page_no, text, redacted_text, is_ocr FROM pages WHERE contract_id=? ORDER BY page_no", (cid,))


@app.get("/api/stats")
def stats(user=Depends(current_user)):
    by_status = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM contracts GROUP BY status")}
    parties = [r["party_b"] for r in db.q("SELECT DISTINCT party_b FROM contracts WHERE party_b IS NOT NULL ORDER BY party_b")]
    years = sorted({(r["y"] or "")[:4] for r in db.q(
        "SELECT coalesce(signing_date, effective_date, start_date, uploaded_at) y FROM contracts")} - {""}, reverse=True)
    return {"by_status": by_status, "parties": parties, "years": years, "count": sum(by_status.values())}


# ================= export / backup =================
@app.get("/api/export.xlsx")
def export_xlsx(ids: str = "", user=Depends(current_user)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    if ids:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        rows = [db.q("SELECT * FROM contracts WHERE id=?", (i,), one=True) for i in id_list]
        rows = [r for r in rows if r]
    else:
        rows = db.q("SELECT * FROM contracts ORDER BY id")
    auth.log(user, "export", detail={"contracts": len(rows)})
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
                     r.get("uploaded_by"), r.get("confirmed_by"), r.get("confirmed_at"),
                     d.get("summary_zh"), r["file_name"]])
    sheet(ws, ["ID", "合同号 No.", "名称 Title", "中文名称", "类型 Type", "甲方 Party A", "乙方 Party B", "签署日期 Signed",
               "生效日期 Effective", "开始 Start", "结束 End", "期限 Term", "币种 Currency", "总金额 Total",
               "结算币种 Settlement", "处理状态 Status", "合同状态 Lifecycle", "上传人 Uploaded by", "复核人 Reviewed by",
               "复核时间 Reviewed at", "中文摘要 Summary", "文件 File"],
          data, [6, 18, 40, 30, 10, 30, 30, 12, 12, 12, 12, 30, 8, 14, 10, 10, 10, 12, 12, 18, 60, 40])

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
def backup(user=Depends(admin)):
    auth.log(user, "backup")
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


# ================= settings (admin) =================
def _public_cfg(cfg):
    c = dict(cfg)
    key = c.get("api_key") or ""
    c["api_key_set"] = bool(key)
    c["api_key"] = (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("…" if key else "")
    return c


@app.get("/api/settings")
def get_settings(user=Depends(admin)):
    return {"config": _public_cfg(llm.load_config()), "providers": llm.PROVIDERS}


@app.put("/api/settings")
def put_settings(body: dict = Body(...), user=Depends(admin)):
    before = llm.load_config()
    cfg = llm.save_config(body)
    changed = {k: [before.get(k), cfg.get(k)] for k in cfg if k != "api_key" and before.get(k) != cfg.get(k)}
    if before.get("api_key") != cfg.get("api_key"):
        changed["api_key"] = "changed"
    if changed:
        auth.log(user, "settings", detail=changed)
    return {"config": _public_cfg(cfg)}


@app.post("/api/settings/test")
def test_settings(body: dict = Body(default={}), user=Depends(admin)):
    cfg = llm.load_config()
    for k, v in (body or {}).items():
        if v not in (None, "", "__keep__"):
            cfg[k] = v
    try:
        out = llm.test_connection(cfg)
        return {"ok": True, "reply": out[:200]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:500]}, status_code=200)


# ================= static =================
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
