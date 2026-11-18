import asyncio
import hashlib
import io
import queue
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import access, agent, auth, captcha, db, llm, payments, pipeline
from .auth import current_user, require

STATIC = Path(__file__).resolve().parent / "static"
ALLOWED = {".pdf", ".docx", ".txt"}

app = FastAPI(title="Contract Management System")
admin = require("admin")
ai_user = require("ai.use")


@app.on_event("startup")
def _startup():
    db.init()      # creates or upgrades the database (schema.py)
    auth.init()    # built-in roles
    pipeline.start()


def _get(cid, user=None):
    row = db.q("SELECT * FROM contracts WHERE id=?", (cid,), one=True)
    if not row or (user is not None and not access.can_view(user, cid)):
        raise HTTPException(404, "Contract not found")
    return row


WORKING = ("queued", "reading", "extracting")


def _require_idle(row):
    """Contracts that are being read or analysed cannot be edited, reviewed, reprocessed or deleted."""
    if row.get("status") in WORKING:
        raise HTTPException(409, "合同正在处理中，请等处理完成后再操作 / This contract is being processed, try again when it is done")


_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s'\"]+")


def _list_row(r):
    r = dict(r)
    r.pop("data_json", None)
    r.pop("search_text", None)
    if r.get("error"):  # errors saved by older versions could contain server file paths
        r["error"] = _PATH_RE.sub("…", r["error"])
    return r


def _name(user):
    return user.get("display_name") or user.get("username")


# ================= auth =================
def _set_cookie(request: Request, resp: Response, token: str):
    # behind Nginx/HTTPS the cookie is only ever sent over encrypted connections.
    # No max_age: the browser forgets it when it is closed (the server also ends idle logins, see auth.IDLE_MINUTES)
    secure = request.url.scheme == "https"
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure)


def _require_captcha(body):
    if not captcha.check(body.get("captcha_id"), body.get("captcha")):
        raise HTTPException(400, "验证码错误或已过期，请重新输入 / Wrong or expired verification code")


# ---- brute-force protection: too many wrong passwords -> short lockout ----
_FAILS = {}
MAX_FAILS, WINDOW_SEC, LOCK_SEC = 8, 15 * 60, 15 * 60


def _client_ip(request: Request):
    return request.client.host if request.client else "?"


def _check_locked(*keys):
    import time
    now = time.time()
    for k in keys:
        rec = _FAILS.get(k)
        if rec and rec.get("locked_until", 0) > now:
            mins = int((rec["locked_until"] - now) // 60) + 1
            raise HTTPException(429, f"尝试次数过多，请 {mins} 分钟后再试 / Too many attempts, try again in {mins} min")


def _record_fail(*keys):
    import time
    now = time.time()
    for k in keys:
        rec = _FAILS.setdefault(k, {"times": []})
        rec["times"] = [t for t in rec["times"] if now - t < WINDOW_SEC] + [now]
        if len(rec["times"]) >= MAX_FAILS:
            rec["locked_until"] = now + LOCK_SEC
            rec["times"] = []


@app.get("/api/auth/state")
def auth_state(request: Request):
    try:
        user = current_user(request)
    except HTTPException:
        user = None
    return {"needs_setup": auth.user_count() == 0, "user": auth.public_user(user)}


@app.get("/api/auth/captcha")
def new_captcha():
    cid, image = captcha.new()
    return JSONResponse({"id": cid, "image": image}, headers={"Cache-Control": "no-store"})


@app.post("/api/auth/setup")
def auth_setup(request: Request, response: Response, body: dict = Body(...)):
    if auth.user_count() > 0:
        raise HTTPException(400, "Already set up")
    u = auth.create_user(body.get("username"), body.get("password"), "admin", body.get("display_name"), body.get("employee_no"))
    auth.log(u, "setup")
    _set_cookie(request, response, auth.new_session(u["id"]))
    return {"user": auth.public_user(u)}


@app.post("/api/auth/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    # people sign in with their employee ID ("account"); "username" is accepted from older pages
    name = (body.get("account") or body.get("username") or "").strip()
    ip = _client_ip(request)
    # lock per IP and per account-from-that-IP, so nobody can lock a real user out from elsewhere
    keys = ("ip:" + ip, "user:" + name.lower() + "@" + ip)
    _check_locked(*keys)
    _require_captcha(body)
    u = auth.find_login(name)
    if u and u.get("status") == "pending" and auth.check_password(body.get("password") or "", u["password_hash"]):
        raise HTTPException(403, "账号正在等待管理员审核，审核通过后才能登录 / Your account is waiting for administrator approval")
    if not u or not u["active"] or u.get("status") != "active" \
            or not auth.check_password(body.get("password") or "", u["password_hash"]):
        _record_fail(*keys)
        auth.log(None, "login_failed", detail={"username": name, "ip": _client_ip(request)})
        raise HTTPException(401, "工号或密码错误 / Wrong employee ID or password")
    for k in keys:
        _FAILS.pop(k, None)
    db.ex("UPDATE users SET last_login=? WHERE id=?", (db.now(), u["id"]))
    auth.log(u, "login", detail={"ip": _client_ip(request)})
    _set_cookie(request, response, auth.new_session(u["id"]))
    return {"user": auth.public_user(u)}


_REG = {}
REG_PER_IP_HOUR, MAX_PENDING = 5, 50


@app.post("/api/auth/register")
def register(request: Request, body: dict = Body(...)):
    import time
    if auth.user_count() == 0:
        raise HTTPException(400, "系统尚未初始化 / Not set up yet")
    ip = _client_ip(request)
    now = time.time()
    recent = [t for t in _REG.get(ip, []) if now - t < 3600]
    if len(recent) >= REG_PER_IP_HOUR:
        raise HTTPException(429, "注册次数过多，请稍后再试 / Too many registrations, try again later")
    _require_captcha(body)
    pending =db.q("SELECT count(*) n FROM users WHERE status='pending'", one=True)["n"]
    if pending >= MAX_PENDING:
        raise HTTPException(429, "待审核的申请太多，请联系管理员 / Too many pending requests, contact the administrator")
    if body.get("password") != body.get("password2", body.get("password")):
        raise HTTPException(400, "两次输入的密码不一致 / Passwords do not match")
    if not (body.get("display_name") or "").strip():
        raise HTTPException(400, "请填写姓名 / Name required")
    u = auth.create_user(body.get("username"), body.get("password"), "viewer", body.get("display_name"),
                         body.get("employee_no"), status="pending")
    _REG[ip] = recent + [now]
    auth.log(None, "register", detail={"username": u["username"], "employee_no": u["employee_no"],
                                       "name": u["display_name"], "ip": ip})
    return {"ok": True, "status": "pending"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.end_session(token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(response: Response, body: dict = Body(...), user=Depends(current_user)):
    if not auth.check_password(body.get("old_password") or "", user["password_hash"]):
        raise HTTPException(400, "原密码不正确 / Current password is wrong")
    auth.validate_password(body.get("new_password"))
    db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(body["new_password"]), user["id"]))
    # every login of this user ends, including this one: sign in again with the new password
    db.ex("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    auth.log(user, "password_change")
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True, "relogin": True}


# ================= users (admin) =================
@app.get("/api/users")
def list_users(user=Depends(admin)):
    return [auth.public_user(u) for u in db.q("SELECT * FROM users WHERE status='active' ORDER BY id")]


@app.get("/api/users/pending")
def pending_users(user=Depends(admin)):
    return [auth.public_user(u) for u in db.q("SELECT * FROM users WHERE status='pending' ORDER BY id")]


@app.get("/api/users/pending/count")
def pending_count(user=Depends(admin)):
    return {"count": db.q("SELECT count(*) n FROM users WHERE status='pending'", one=True)["n"]}


@app.post("/api/users/{uid}/approve")
def approve_user(uid: int, body: dict = Body(default={}), user=Depends(admin)):
    u = db.q("SELECT * FROM users WHERE id=? AND status='pending'", (uid,), one=True)
    if not u:
        raise HTTPException(404, "Request not found")
    role_key = (body or {}).get("role") or "viewer"
    if not auth.role(role_key):
        raise HTTPException(400, "Invalid role")
    dept_id = (body or {}).get("department_id") or None
    if dept_id and not access.department(dept_id):
        raise HTTPException(400, "Department not found")
    db.ex("UPDATE users SET status='active', role=?, department_id=?, position=? WHERE id=?",
          (role_key, dept_id, (body or {}).get("position") or None, uid))
    auth.log(user, "user_approve", detail={"username": u["username"], "employee_no": u["employee_no"],
                                           "role": role_key, "department": (access.department(dept_id) or {}).get("name")})
    return auth.public_user(db.q("SELECT * FROM users WHERE id=?", (uid,), one=True))


@app.post("/api/users/{uid}/reject")
def reject_user(uid: int, user=Depends(admin)):
    u = db.q("SELECT * FROM users WHERE id=? AND status='pending'", (uid,), one=True)
    if not u:
        raise HTTPException(404, "Request not found")
    db.ex("DELETE FROM users WHERE id=?", (uid,))
    auth.log(user, "user_reject", detail={"username": u["username"], "employee_no": u["employee_no"]})
    return {"ok": True}


@app.post("/api/users")
def add_user(body: dict = Body(...), user=Depends(admin)):
    u = auth.create_user(body.get("username"), body.get("password"), body.get("role") or "viewer", body.get("display_name"),
                         body.get("employee_no"))
    dept_id = body.get("department_id") or None
    if dept_id and not access.department(dept_id):
        raise HTTPException(400, "Department not found")
    db.ex("UPDATE users SET department_id=?, position=? WHERE id=?", (dept_id, body.get("position") or None, u["id"]))
    u = db.q("SELECT * FROM users WHERE id=?", (u["id"],), one=True)
    auth.log(user, "user_create", detail={"username": u["username"], "role": u["role"], "employee_no": u["employee_no"],
                                          "department": (access.department(dept_id) or {}).get("name"),
                                          "position": u["position"]})
    return auth.public_user(u)


@app.put("/api/users/{uid}")
def update_user(uid: int, body: dict = Body(...), user=Depends(admin)):
    u = db.q("SELECT * FROM users WHERE id=? AND status='active'", (uid,), one=True)
    if not u:
        raise HTTPException(404, "User not found")
    changes = {}
    if "role" in body and body["role"] != u["role"]:
        if not auth.role(body["role"]):
            raise HTTPException(400, "Invalid role")
        changes["role"] = [u["role"], body["role"]]
    if "active" in body and int(bool(body["active"])) != u["active"]:
        changes["active"] = [u["active"], int(bool(body["active"]))]
    if "display_name" in body and (body["display_name"] or "") != (u["display_name"] or ""):
        changes["display_name"] = [u["display_name"], body["display_name"]]
    if "department_id" in body and (body["department_id"] or None) != u.get("department_id"):
        if body["department_id"] and not access.department(body["department_id"]):
            raise HTTPException(400, "Department not found")
        changes["department_id"] = [u.get("department_id"), body["department_id"] or None]
    if "employee_no" in body and auth.normalize_employee_no(body["employee_no"]) != (u.get("employee_no") or ""):
        new_no = auth.normalize_employee_no(body["employee_no"])
        auth.validate_employee_no(new_no, exclude_user_id=uid)
        changes["employee_no"] = [u.get("employee_no"), new_no]
    if "position" in body and (body["position"] or "") != (u.get("position") or ""):
        changes["position"] = [u.get("position"), body["position"] or None]
    for k in ("ai_daily_questions", "ai_daily_tokens"):
        if k in body:
            v = int(body[k]) if body[k] not in (None, "") else None
            if v != u.get(k):
                changes[k] = [u.get(k), v]
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
        shown = dict(changes)
        if "department_id" in shown:
            shown["department"] = [(access.department(x) or {}).get("name") for x in shown.pop("department_id")]
        auth.log(user, "user_update", detail={"username": u["username"], "changes": shown})
    return auth.public_user(db.q("SELECT * FROM users WHERE id=?", (uid,), one=True))


# ================= roles (admin) =================
@app.get("/api/roles")
def list_roles(user=Depends(current_user)):
    return {"roles": auth.roles(), "permissions": auth.PERMISSIONS}


def _clean_perms(perms):
    return [p for p in (perms or []) if p in auth.PERMISSIONS]


@app.post("/api/roles")
def add_role(body: dict = Body(...), user=Depends(admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "角色名称不能为空 / Name required")
    if db.q("SELECT 1 FROM roles WHERE lower(name)=lower(?)", (name,), one=True):
        raise HTTPException(400, "角色已存在 / Role already exists")
    import secrets as _s
    key = "r_" + _s.token_hex(4)
    perms = _clean_perms(body.get("permissions"))
    db.ex("INSERT INTO roles (key, name, permissions, builtin, created_at) VALUES (?,?,?,0,?)",
          (key, name, json.dumps(perms), db.now()))
    auth.log(user, "role_create", detail={"name": name, "permissions": perms})
    return auth.role(key)


@app.put("/api/roles/{key}")
def update_role(key: str, body: dict = Body(...), user=Depends(admin)):
    r = auth.role(key)
    if not r:
        raise HTTPException(404, "Role not found")
    if key == "admin":
        raise HTTPException(400, "管理员角色固定拥有全部权限 / The admin role always has every permission")
    name = (body.get("name") or r["name"]).strip()
    perms = _clean_perms(body.get("permissions", r["permissions"]))
    db.ex("UPDATE roles SET name=?, permissions=? WHERE key=?", (name, json.dumps(perms), key))
    added = [p for p in perms if p not in r["permissions"]]
    removed = [p for p in r["permissions"] if p not in perms]
    auth.log(user, "role_update", detail={"name": name, "added": added, "removed": removed,
                                          **({"renamed": [r["name"], name]} if name != r["name"] else {})})
    return auth.role(key)


@app.delete("/api/roles/{key}")
def delete_role(key: str, user=Depends(admin)):
    r = auth.role(key)
    if not r:
        raise HTTPException(404, "Role not found")
    if r["builtin"]:
        raise HTTPException(400, "内置角色不能删除 / Built-in roles cannot be deleted")
    n = db.q("SELECT count(*) n FROM users WHERE role=?", (key,), one=True)["n"]
    if n:
        raise HTTPException(400, f"还有 {n} 个用户使用该角色，请先调整 / {n} users still have this role")
    db.ex("DELETE FROM roles WHERE key=?", (key,))
    auth.log(user, "role_delete", detail={"name": r["name"]})
    return {"ok": True}


# ================= departments =================
@app.get("/api/departments")
def list_departments(user=Depends(current_user)):
    rows = db.q("SELECT d.*, (SELECT count(*) FROM users u WHERE u.department_id=d.id) users, "
                "(SELECT count(*) FROM contract_departments cd WHERE cd.department_id=d.id) contracts "
                "FROM departments d ORDER BY d.name")
    return rows


@app.post("/api/departments")
def add_department(body: dict = Body(...), user=Depends(admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "部门名称不能为空 / Name required")
    if db.q("SELECT 1 FROM departments WHERE lower(name)=lower(?)", (name,), one=True):
        raise HTTPException(400, "部门已存在 / Department already exists")
    did = db.ex("INSERT INTO departments (name, see_all, created_at) VALUES (?,?,?)",
                (name, 1 if body.get("see_all") else 0, db.now()))
    auth.log(user, "dept_create", detail={"name": name, "see_all": bool(body.get("see_all"))})
    return db.q("SELECT * FROM departments WHERE id=?", (did,), one=True)


@app.put("/api/departments/{did}")
def update_department(did: int, body: dict = Body(...), user=Depends(admin)):
    d = access.department(did)
    if not d:
        raise HTTPException(404, "Department not found")
    name = (body.get("name") or d["name"]).strip()
    see_all = 1 if body.get("see_all", d["see_all"]) else 0
    if name.lower() != d["name"].lower() and db.q("SELECT 1 FROM departments WHERE lower(name)=lower(?)", (name,), one=True):
        raise HTTPException(400, "部门已存在 / Department already exists")
    db.ex("UPDATE departments SET name=?, see_all=? WHERE id=?", (name, see_all, did))
    auth.log(user, "dept_update", detail={"name": [d["name"], name], "see_all": [bool(d["see_all"]), bool(see_all)]})
    return access.department(did)


@app.delete("/api/departments/{did}")
def delete_department(did: int, user=Depends(admin)):
    d = access.department(did)
    if not d:
        raise HTTPException(404, "Department not found")
    n_users = db.q("SELECT count(*) n FROM users WHERE department_id=?", (did,), one=True)["n"]
    if n_users:
        raise HTTPException(400, f"该部门还有 {n_users} 个用户，请先调整他们的部门 / {n_users} users still in this department")
    orphaned = db.q("SELECT contract_id FROM contract_departments WHERE department_id=? AND contract_id NOT IN "
                    "(SELECT contract_id FROM contract_departments WHERE department_id<>?)", (did, did))
    if orphaned:
        raise HTTPException(400, f"有 {len(orphaned)} 份合同只属于这个部门，删除后会变成全公司可见，请先调整这些合同的部门 / "
                                 f"{len(orphaned)} contracts belong only to this department; reassign them first")
    db.ex("DELETE FROM contract_departments WHERE department_id=?", (did,))
    db.ex("DELETE FROM departments WHERE id=?", (did,))
    auth.log(user, "dept_delete", detail={"name": d["name"]})
    return {"ok": True}


@app.put("/api/contracts/{cid}/departments")
def set_contract_departments(cid: int, body: dict = Body(...), user=Depends(require("contract.assign_dept"))):
    _get(cid, user)
    before = [d["name"] for d in access.departments_of(cid)]
    new_ids = access.set_departments(cid, body.get("department_ids") or [])
    after = [d["name"] for d in access.departments_of(cid)]
    if before != after:
        auth.log(user, "departments", cid, {"old": before, "new": after})
    return access.departments_of(cid) if new_ids is not None else []


# ================= audit =================
@app.get("/api/audit")
def audit(user_q: str = "", action: str = "", contract_id: int = 0, date_from: str = "", date_to: str = "",
          limit: int = 200, offset: int = 0, user=Depends(require("audit.view"))):
    sql, args = "FROM audit_log WHERE 1=1", []
    if user["role"] != "admin":
        cond, cargs = access.visible_sql(user, "c")
        sql += f" AND (user_id=? OR contract_id IN (SELECT c.id FROM contracts c WHERE {cond}))"
        args += [user["id"]] + cargs
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
    access.require_view(user, cid)
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


# the year a contract is filed under: signing date, else effective date, else start date, else upload date
YEAR_SQL = "substr(coalesce(contracts.signing_date, contracts.effective_date, contracts.start_date, contracts.uploaded_at),1,4)"


def _contract_filter(user, q="", type="", party="", status="", lifecycle="", year="", dept="", project=""):
    """WHERE clause (and args) for the contract list and the Excel export, limited to what the user may see."""
    cond, args = access.visible_sql(user, "contracts")
    sql = cond
    if dept == "none":
        sql += " AND NOT EXISTS (SELECT 1 FROM contract_departments cd WHERE cd.contract_id=contracts.id)"
    elif dept:
        if not str(dept).isdigit():
            raise HTTPException(400, "Invalid department")
        sql += " AND EXISTS (SELECT 1 FROM contract_departments cd WHERE cd.contract_id=contracts.id AND cd.department_id=?)"
        args.append(int(dept))
    for word in q.split():
        like = f"%{word.lower()}%"
        # search_text: file name, number, titles, parties, notes and extracted content (see db.search_text_for)
        sql += (" AND (coalesce(contracts.search_text,'') LIKE ?"
                " OR contracts.id IN (SELECT contract_id FROM pages WHERE lower(text) LIKE ?))")
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
        sql += f" AND {YEAR_SQL}=?"
        args.append(str(year))
    if project == "none":
        sql += " AND contracts.project_id IS NULL"
    elif project:
        if not str(project).isdigit():
            raise HTTPException(400, "Invalid project")
        sql += " AND contracts.project_id=?"
        args.append(int(project))
    return sql, args


PROJECT_NAME_SQL = "(SELECT name FROM projects WHERE projects.id=contracts.project_id) AS project_name"


CONTRACT_ORDER = f" ORDER BY {YEAR_SQL} DESC, contracts.id DESC"


@app.get("/api/contracts")
def list_contracts(q: str = "", type: str = "", party: str = "", status: str = "", lifecycle: str = "", year: str = "",
                   dept: str = "", project: str = "", page: int = 0, page_size: int = 0, user=Depends(current_user)):
    """Newest year first. With page_size the answer is one page: {"total", "page", "page_size", "rows"};
    without it, the whole list (used by the AI assistant's contract picker)."""
    where, args = _contract_filter(user, q, type, party, status, lifecycle, year, dept, project)
    sql = f"SELECT contracts.*, {YEAR_SQL} AS year, {PROJECT_NAME_SQL} FROM contracts WHERE {where}{CONTRACT_ORDER}"
    total = None
    if page_size > 0:
        page_size = min(page_size, 200)
        page = max(page, 1)
        total = db.q(f"SELECT count(*) n FROM contracts WHERE {where}", args, one=True)["n"]
        sql += " LIMIT ? OFFSET ?"
        args = args + [page_size, (page - 1) * page_size]
    found = db.q(sql, args)
    depts = access.departments_map([r["id"] for r in found])
    pays = payments.summaries([r["id"] for r in found])
    words = q.split()
    out = []
    for r in found:
        row = _list_row(r)
        row["departments"] = depts.get(r["id"], [])
        row["payments"] = pays.get(r["id"])
        if words:
            row["hits"] = _hits(r["id"], json.loads(r["data_json"]) if r.get("data_json") else {}, words)
        out.append(row)
    if total is None:
        return out
    return {"total": total, "page": page, "page_size": page_size, "rows": out}


@app.post("/api/contracts")
async def upload(files: List[UploadFile] = File(...), user=Depends(require("contract.upload"))):
    from .extract_text import UnreadableFile, check_readable
    created, duplicates, rejected = [], [], []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED:
            rejected.append({"file": f.filename, "reason": "只支持 PDF、Word（.docx）和 TXT 文件 / Only PDF, Word (.docx) and TXT files"})
            continue
        content = await f.read()
        try:
            check_readable(ext, content)  # damaged files are refused here, before they reach the ledger
        except UnreadableFile as e:
            rejected.append({"file": f.filename, "reason": str(e)})
            continue
        h = hashlib.sha256(content).hexdigest()
        dup = db.q("SELECT id, file_name FROM contracts WHERE file_hash=?", (h,), one=True)
        if dup:
            duplicates.append({"file": f.filename, "existing_id": dup["id"]})
            continue
        stored = f"{datetime.now():%Y%m%d%H%M%S}_{h[:10]}{ext}"
        (db.FILES_DIR / stored).write_bytes(content)
        cid = db.ex("INSERT INTO contracts (file_name, stored_name, file_hash, uploaded_at, updated_at, status, uploaded_by) "
                    "VALUES (?,?,?,?,?, 'queued', ?)", (f.filename, stored, h, db.now(), db.now(), _name(user)))
        db.refresh_search(cid)
        access.assign_default(cid, user)
        auth.log(user, "upload", cid, {"file": f.filename}, label=f.filename)
        pipeline.enqueue(cid, "read")
        created.append(cid)
    return {"created": created, "duplicates": duplicates, "rejected": rejected}


@app.get("/api/contracts/{cid}")
def get_contract(cid: int, user=Depends(current_user)):
    row = _get(cid, user)
    data = json.loads(row["data_json"]) if row.get("data_json") else None
    row = _list_row(row)
    row["data"] = data
    row["departments"] = access.departments_of(cid)
    p = db.q("SELECT id, name, code FROM projects WHERE id=?", (row.get("project_id"),), one=True) if row.get("project_id") else None
    row["project"] = p
    return row


@app.put("/api/contracts/{cid}")
def update_contract(cid: int, body: dict = Body(...), user=Depends(require("contract.edit"))):
    row = _get(cid, user)
    if "data" in body and body["data"] is not None:
        _require_idle(row)  # notes and contract status may still be changed while the AI is working
        old = json.loads(row["data_json"]) if row.get("data_json") else {}
        changes = auth.diff(old, body["data"])
        if changes:
            db.save_data(cid, body["data"])
            payments.sync_from_extraction(cid, body["data"])  # recorded payment nodes are kept
            db.ex("UPDATE contracts SET updated_by=? WHERE id=?", (_name(user), cid))
            reopened = row["status"] == "confirmed"
            if reopened:  # a confirmed contract that is edited must be reviewed again
                db.ex("UPDATE contracts SET status='review', confirmed_by=NULL, confirmed_at=NULL WHERE id=?", (cid,))
            auth.log(user, "edit", cid, {"changes": changes, "reopened": reopened})
    for k in ("lifecycle_status", "notes"):
        if k in body and (body[k] or "") != (row.get(k) or ""):
            db.ex(f"UPDATE contracts SET {k}=?, updated_at=?, updated_by=? WHERE id=?", (body[k], db.now(), _name(user), cid))
            if k == "notes":
                db.refresh_search(cid)
            auth.log(user, k, cid, {"old": row.get(k), "new": body[k]})
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/confirm")
def confirm(cid: int, user=Depends(require("contract.review"))):
    row = _get(cid, user)
    _require_idle(row)
    if not row.get("data_json"):
        raise HTTPException(400, "Nothing to confirm")
    db.ex("UPDATE contracts SET status='confirmed', confirmed_at=?, confirmed_by=?, updated_at=? WHERE id=?",
          (db.now(), _name(user), db.now(), cid))
    auth.log(user, "confirm", cid)
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/unconfirm")
def unconfirm(cid: int, user=Depends(require("contract.review"))):
    _require_idle(_get(cid, user))
    db.ex("UPDATE contracts SET status='review', confirmed_at=NULL, confirmed_by=NULL, updated_at=? WHERE id=?", (db.now(), cid))
    auth.log(user, "unconfirm", cid)
    return get_contract(cid, user)


@app.post("/api/contracts/{cid}/reprocess")
def reprocess(cid: int, stage: str = "extract", user=Depends(require("contract.reprocess"))):
    _require_idle(_get(cid, user))
    db.ex("UPDATE contracts SET confirmed_at=NULL, confirmed_by=NULL WHERE id=?", (cid,))
    auth.log(user, "reread" if stage == "read" else "reextract", cid)
    pipeline.enqueue(cid, "read" if stage == "read" else "extract")
    return {"ok": True}


@app.delete("/api/contracts/{cid}")
def delete(cid: int, user=Depends(require("contract.delete"))):
    row = _get(cid, user)
    _require_idle(row)
    auth.log(user, "delete", cid, {"file": row["file_name"]})
    payments.delete_contract_files(cid)
    db.delete_contract(cid)
    try:
        (db.FILES_DIR / row["stored_name"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/contracts/{cid}/file")
def get_file(cid: int, download: int = 0, user=Depends(current_user)):
    row = _get(cid, user)
    path = db.FILES_DIR / row["stored_name"]
    media = "application/pdf" if path.suffix.lower() == ".pdf" else None
    if download and "contract.download" not in auth.perms_of(user):
        raise HTTPException(403, "没有下载原件的权限 / Permission denied")
    if download:
        return FileResponse(path, media_type=media, filename=row["file_name"])
    return FileResponse(path, media_type=media,
                        headers={"Content-Disposition": "inline; filename*=UTF-8''contract" + path.suffix})


@app.get("/api/contracts/{cid}/pages")
def get_pages(cid: int, user=Depends(current_user)):
    _get(cid, user)
    return db.q("SELECT page_no, text, redacted_text, is_ocr FROM pages WHERE contract_id=? ORDER BY page_no", (cid,))


@app.get("/api/stats")
def stats(user=Depends(current_user)):
    cond, a = access.visible_sql(user, "contracts")
    by_status = {r["status"]: r["n"] for r in db.q(f"SELECT status, count(*) n FROM contracts WHERE {cond} GROUP BY status", a)}
    parties = [r["party_b"] for r in db.q(
        f"SELECT DISTINCT party_b FROM contracts WHERE party_b IS NOT NULL AND {cond} ORDER BY party_b", a)]
    years = sorted({(r["y"] or "")[:4] for r in db.q(
        f"SELECT coalesce(signing_date, effective_date, start_date, uploaded_at) y FROM contracts WHERE {cond}", a)} - {""},
        reverse=True)
    return {"by_status": by_status, "parties": parties, "years": years, "count": sum(by_status.values())}


# ================= projects =================
def _project_body(body, current=None):
    name = (body.get("name") if "name" in body else (current or {}).get("name") or "")
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "项目名称不能为空 / Project name required")
    if len(name) > 100:
        raise HTTPException(400, "项目名称最多 100 字 / Project name: at most 100 characters")
    dup = db.q("SELECT id FROM projects WHERE lower(name)=lower(?)", (name,), one=True)
    if dup and (not current or dup["id"] != current["id"]):
        raise HTTPException(400, "已有同名项目 / A project with this name already exists")
    code = (body.get("code") if "code" in body else (current or {}).get("code")) or None
    note = (body.get("note") if "note" in body else (current or {}).get("note")) or None
    return name, (code.strip()[:50] or None) if code else None, (note.strip()[:500] or None) if note else None


def _project(pid):
    p = db.q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p:
        raise HTTPException(404, "Project not found")
    return p


@app.get("/api/projects")
def list_projects(user=Depends(current_user)):
    """Every project, with figures over the contracts this user may see: contracts, contract value per currency,
    payment progress (complete payments / nodes) and overdue nodes."""
    cond, args = access.visible_sql(user, "contracts")
    ok = payments.paid_sql("p")
    today = payments.today()
    stats = {r["project_id"]: r for r in db.q(
        f"SELECT contracts.project_id, count(DISTINCT contracts.id) contracts, count(p.id) nodes, coalesce(sum({ok}), 0) paid, "
        f"coalesce(sum(NOT {ok} AND p.status<>'paid' AND p.due_date IS NOT NULL AND p.due_date<?), 0) overdue "
        f"FROM contracts LEFT JOIN payments p ON p.contract_id=contracts.id "
        f"WHERE contracts.project_id IS NOT NULL AND {cond} GROUP BY contracts.project_id", [today] + args)}
    values = {}
    for r in db.q(f"SELECT project_id, currency, sum(total_amount) total FROM contracts WHERE project_id IS NOT NULL "
                  f"AND total_amount IS NOT NULL AND {cond} GROUP BY project_id, currency ORDER BY currency", args):
        values.setdefault(r["project_id"], []).append({"currency": r["currency"] or "", "total": round(r["total"], 2)})
    out = []
    for p in db.q("SELECT * FROM projects ORDER BY name COLLATE NOCASE"):
        s = stats.get(p["id"]) or {}
        out.append(dict(p, contracts=s.get("contracts", 0), nodes=s.get("nodes", 0), paid=s.get("paid", 0),
                        overdue=s.get("overdue", 0), values=values.get(p["id"], [])))
    none = db.q(f"SELECT count(*) n FROM contracts WHERE project_id IS NULL AND {cond}", args, one=True)["n"]
    return {"projects": out, "without_project": none}


@app.post("/api/projects")
def add_project(body: dict = Body(...), user=Depends(require("project.manage"))):
    name, code, note = _project_body(body)
    pid = db.ex("INSERT INTO projects (name, code, note, created_at, created_by) VALUES (?,?,?,?,?)",
                (name, code, note, db.now(), _name(user)))
    auth.log(user, "project_create", detail={"name": name, "code": code})
    return _project(pid)


@app.put("/api/projects/{pid}")
def update_project(pid: int, body: dict = Body(...), user=Depends(require("project.manage"))):
    p = _project(pid)
    name, code, note = _project_body(body, p)
    db.ex("UPDATE projects SET name=?, code=?, note=? WHERE id=?", (name, code, note, pid))
    changes = {k: [p[k], v] for k, v in (("name", name), ("code", code), ("note", note)) if (p[k] or None) != v}
    if changes:
        auth.log(user, "project_update", detail={"name": name, "changes": changes})
    return _project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: int, user=Depends(require("project.manage"))):
    p = _project(pid)
    n = db.q("SELECT count(*) n FROM contracts WHERE project_id=?", (pid,), one=True)["n"]
    db.ex("DELETE FROM projects WHERE id=?", (pid,))  # its contracts stay, without a project
    auth.log(user, "project_delete", detail={"name": p["name"], "contracts": n})
    return {"ok": True, "contracts": n}


@app.put("/api/contracts/{cid}/project")
def set_contract_project(cid: int, body: dict = Body(...), user=Depends(require("contract.edit"))):
    """Put a contract into a project (or none). Does not change its review status."""
    row = _get(cid, user)
    pid = body.get("project_id") or None
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid project")
        _project(pid)
    if pid != row.get("project_id"):
        old = db.q("SELECT name FROM projects WHERE id=?", (row.get("project_id"),), one=True) if row.get("project_id") else None
        db.ex("UPDATE contracts SET project_id=?, updated_at=?, updated_by=? WHERE id=?", (pid, db.now(), _name(user), cid))
        new = _project(pid)["name"] if pid else None
        auth.log(user, "project", cid, {"old": old and old["name"], "new": new})
    return get_contract(cid, user)


# ================= payments =================
pay_user = require("payment.record")


def _payment(pid, user):
    """The payment node, if the user may see its contract."""
    p = payments.get(pid)
    if not access.can_view(user, p["contract_id"]):
        raise HTTPException(404, "Payment not found")
    return p


def _pay_label(p):
    return f"#{p['seq']} " + (p.get("trigger_zh") or p.get("trigger_en") or "")


@app.get("/api/contracts/{cid}/payments")
def contract_payments(cid: int, user=Depends(current_user)):
    _get(cid, user)
    return {"rows": payments.for_contract(cid), "today": payments.today()}


@app.post("/api/contracts/{cid}/payments")
def add_payment(cid: int, body: dict = Body(...), user=Depends(pay_user)):
    _get(cid, user)
    p = payments.create(cid, body, _name(user))
    auth.log(user, "payment_add", cid, {"payment": _pay_label(p), "amount": p["amount"], "status": p["status"]})
    return p


@app.put("/api/payments/{pid}")
def update_payment(pid: int, body: dict = Body(...), user=Depends(pay_user)):
    _payment(pid, user)
    p, changes = payments.update(pid, body, _name(user))
    if changes:
        auth.log(user, "payment_update", p["contract_id"], {"payment": _pay_label(p), "changes": changes})
    return p


@app.delete("/api/payments/{pid}")
def delete_payment(pid: int, user=Depends(pay_user)):
    p = _payment(pid, user)
    payments.delete(pid)
    auth.log(user, "payment_delete", p["contract_id"], {"payment": _pay_label(p), "status": p["status"]})
    return {"ok": True}


@app.post("/api/payments/{pid}/files")
async def upload_payment_files(pid: int, files: List[UploadFile] = File(...), user=Depends(pay_user)):
    p = _payment(pid, user)
    out = []
    for f in files:
        content = await f.read()
        rec = payments.add_file(pid, f.filename, content, _name(user))
        auth.log(user, "payment_file_add", p["contract_id"], {"payment": _pay_label(p), "file": rec["file_name"]})
        out.append(rec)
    return out


@app.get("/api/payment-files/{fid}")
def get_payment_file(fid: int, download: int = 0, user=Depends(current_user)):
    f = payments.get_file(fid)
    if not access.can_view(user, f["contract_id"]):
        raise HTTPException(404, "File not found")
    path = payments.proof_dir() / f["stored_name"]
    if not path.exists():
        raise HTTPException(404, "File not found")
    media = payments.media_type(f["stored_name"])
    headers = {"X-Content-Type-Options": "nosniff"}
    if download or media not in payments.INLINE_TYPES:
        return FileResponse(path, media_type=media or "application/octet-stream", filename=f["file_name"], headers=headers)
    headers["Content-Disposition"] = "inline; filename*=UTF-8''proof" + path.suffix
    return FileResponse(path, media_type=media, headers=headers)


@app.delete("/api/payment-files/{fid}")
def delete_payment_file(fid: int, user=Depends(pay_user)):
    f = payments.get_file(fid)
    if not access.can_view(user, f["contract_id"]):
        raise HTTPException(404, "File not found")
    p = payments.get(f["payment_id"])
    payments.delete_file(fid)
    auth.log(user, "payment_file_delete", f["contract_id"], {"payment": _pay_label(p), "file": f["file_name"]})
    return {"ok": True}


@app.get("/api/payments/contracts")
def payment_contracts(status: str = "pending", q: str = "", year: str = "", project: str = "", page: int = 1,
                      page_size: int = 20, user=Depends(current_user)):
    """Payment tracking grouped by contract: one row per contract that has payment nodes.
    status: pending (something left to pay) / overdue / due30 / incomplete / paid (everything paid) / "" (all)."""
    where, args = _contract_filter(user, year=year, project=project)
    for word in q.split():
        like = f"%{word.lower()}%"
        where += (" AND (coalesce(contracts.search_text,'') LIKE ? OR contracts.id IN (SELECT contract_id FROM payments "
                  "WHERE lower(coalesce(trigger_en,'')||' '||coalesce(trigger_zh,'')||' '||coalesce(note,'')) LIKE ?))")
        args += [like, like]
    today = payments.today()
    ok = payments.paid_sql("p")
    grouped = (f"SELECT p.contract_id, count(*) nodes, sum({ok}) paid, sum(p.status='paid' AND NOT {ok}) incomplete, "
               f"sum(NOT {ok} AND p.status<>'paid' AND p.due_date IS NOT NULL AND p.due_date<?) overdue, "
               f"sum(NOT {ok} AND p.status<>'paid' AND p.due_date>=? AND p.due_date<=date(?, '+30 days')) due30, "
               f"min(CASE WHEN NOT {ok} THEN p.due_date END) next_due "
               f"FROM payments p JOIN contracts ON contracts.id=p.contract_id WHERE {where} GROUP BY p.contract_id")
    gargs = [today, today, today] + args
    having = {"pending": "paid < nodes", "overdue": "overdue > 0", "due30": "due30 > 0", "incomplete": "incomplete > 0",
              "paid": "paid = nodes"}.get(status, "1=1")
    total = db.q(f"SELECT count(*) n FROM ({grouped}) g WHERE {having}", gargs, one=True)["n"]
    page_size, page = min(max(page_size, 1), 100), max(page, 1)
    rows = db.q(f"SELECT g.*, c.contract_no, c.title, c.title_zh, c.party_b, c.currency, "
                f"(SELECT name FROM projects WHERE projects.id=c.project_id) project_name FROM ({grouped}) g "
                f"JOIN contracts c ON c.id=g.contract_id WHERE {having} "
                f"ORDER BY g.overdue > 0 DESC, g.next_due IS NULL, g.next_due, g.contract_id DESC LIMIT ? OFFSET ?",
                gargs + [page_size, (page - 1) * page_size])
    # planned / paid / unpaid totals per currency, from the nodes of the contracts on this page
    from decimal import Decimal
    ids = [r["contract_id"] for r in rows]
    sums = {i: {} for i in ids}
    if ids:
        marks = ",".join("?" * len(ids))
        for n in db.q(f"SELECT * FROM payments WHERE contract_id IN ({marks})", ids):
            cur = n["currency"] or ""
            t = sums[n["contract_id"]].setdefault(cur, {"planned": Decimal(0), "paid": Decimal(0)})
            if n["amount"] is not None:
                t["planned"] += Decimal(str(n["amount"]))
            if payments.is_paid(n):
                t["paid"] += Decimal(str(n["paid_amount"]))
    for r in rows:
        r["paid"] = r["paid"] or 0
        r["totals"] = [{"currency": cur, "planned": float(t["planned"]), "paid": float(t["paid"]),
                        "unpaid": float(max(t["planned"] - t["paid"], Decimal(0)))}
                       for cur, t in sorted(sums[r["contract_id"]].items())]
    return {"total": total, "page": page, "page_size": page_size, "rows": rows, "today": today}


@app.get("/api/payments")
def payment_tracking(status: str = "", q: str = "", year: str = "", project: str = "", page: int = 1,
                     page_size: int = 20, user=Depends(current_user)):
    """Payment nodes of every contract the user may see. status: pending / paid / overdue / due30 (due in 30 days)."""
    where, args = _contract_filter(user, year=year, project=project)
    sql = f"FROM payments p JOIN contracts ON contracts.id=p.contract_id WHERE {where}"
    today = payments.today()
    ok = payments.paid_sql("p")
    if status == "paid":
        sql += f" AND {ok}"
    elif status == "pending":            # everything not fully paid, including records that need completing
        sql += f" AND NOT {ok}"
    elif status == "incomplete":
        sql += f" AND p.status='paid' AND NOT {ok}"
    elif status == "overdue":
        sql += f" AND NOT {ok} AND p.status<>'paid' AND p.due_date IS NOT NULL AND p.due_date<?"
        args.append(today)
    elif status == "due30":
        sql += (f" AND NOT {ok} AND p.status<>'paid' AND p.due_date IS NOT NULL AND p.due_date>=? "
                "AND p.due_date<=date(?, '+30 days')")
        args += [today, today]
    for word in q.split():
        like = f"%{word.lower()}%"
        sql += (" AND (coalesce(contracts.search_text,'') LIKE ? OR lower(coalesce(p.trigger_en,'')||' '||"
                "coalesce(p.trigger_zh,'')||' '||coalesce(p.note,'')) LIKE ?)")
        args += [like, like]
    total = db.q("SELECT count(*) n " + sql, args, one=True)["n"]
    page_size, page = min(max(page_size, 1), 200), max(page, 1)
    rows = db.q("SELECT p.*, contracts.contract_no, contracts.title, contracts.title_zh, contracts.party_b, "
                "(SELECT count(*) FROM payment_files f WHERE f.payment_id=p.id) files " + sql +
                f" ORDER BY {ok}, p.due_date IS NULL, p.due_date, contracts.id DESC, p.seq LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size])
    for r in rows:
        r["state"] = payments.state(r, today)
    return {"total": total, "page": page, "page_size": page_size, "rows": rows, "today": today}


# ================= export / backup =================
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _xlsx_safe(v):
    """Text from contracts must never become an Excel formula: prefix risky strings with a quote."""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


@app.get("/api/export.xlsx")
def export_xlsx(ids: str = "", q: str = "", type: str = "", party: str = "", status: str = "", lifecycle: str = "",
                year: str = "", dept: str = "", project: str = "", user=Depends(require("contract.export"))):
    """Export the given ids, or else every contract matching the list filters (all pages, not just the one shown)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    if ids:
        allowed = set(access.visible_ids(user))
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit() and int(x) in allowed]
        rows = [db.q("SELECT * FROM contracts WHERE id=?", (i,), one=True) for i in id_list]
        rows = [r for r in rows if r]
    else:
        where, args = _contract_filter(user, q, type, party, status, lifecycle, year, dept, project)
        rows = db.q(f"SELECT contracts.* FROM contracts WHERE {where}{CONTRACT_ORDER}", args)
    names = {p["id"]: p["name"] for p in db.q("SELECT id, name FROM projects")}
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
            ws.append([_xlsx_safe(v) for v in r])
        for i, w in enumerate(widths):
            ws.column_dimensions[chr(65 + i)].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "合同 Contracts"
    data = []
    for r in rows:
        d = json.loads(r["data_json"]) if r.get("data_json") else {}
        data.append([r["id"], r["contract_no"], names.get(r.get("project_id")), r["title"], r["title_zh"], r["contract_type"], r["party_a"], r["party_b"],
                     r["signing_date"], r["effective_date"], r["start_date"], r["end_date"],
                     (d.get("basic") or {}).get("term_text"), r["currency"], r["total_amount"],
                     (d.get("amount") or {}).get("settlement_currency"), r["status"], r["lifecycle_status"],
                     r.get("uploaded_by"), r.get("confirmed_by"), r.get("confirmed_at"),
                     d.get("summary_zh"), r["file_name"]])
    sheet(ws, ["ID", "合同号 No.", "项目 Project", "名称 Title", "中文名称", "类型 Type", "甲方 Party A", "乙方 Party B", "签署日期 Signed",
               "生效日期 Effective", "开始 Start", "结束 End", "期限 Term", "币种 Currency", "总金额 Total",
               "结算币种 Settlement", "处理状态 Status", "合同状态 Lifecycle", "上传人 Uploaded by", "复核人 Reviewed by",
               "复核时间 Reviewed at", "中文摘要 Summary", "文件 File"],
          data, [6, 18, 24, 40, 30, 10, 30, 30, 12, 12, 12, 12, 30, 8, 14, 10, 10, 10, 12, 12, 18, 60, 40])

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
        for p in payments.export_rows(r["id"]):
            data.append([r["id"], r["contract_no"], r["party_b"]] + p)
    sheet(ws3, ["合同ID", "合同号 No.", "乙方 Party B", "期次", "付款条件 Trigger", "中文", "比例 %", "计划金额 Planned",
                "币种", "付款期限 Due rule", "计划日期 Due date", "状态 Status", "实付金额 Paid", "付款日期 Paid on",
                "备注 Note", "登记人 Recorded by", "凭证数 Proofs", "页码"], data,
          [8, 18, 28, 6, 40, 30, 8, 14, 8, 36, 12, 14, 14, 12, 30, 12, 8, 6])

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
        if llm.CONFIG_PATH.exists():  # settings, so a restore works without setting everything up again
            z.write(llm.CONFIG_PATH, "data/config.json")
        for f in db.FILES_DIR.rglob("*"):  # originals and payment proof files
            if f.is_file():
                z.write(f, "data/files/" + f.relative_to(db.FILES_DIR).as_posix())
    # remove the temporary copy once the download has been sent
    return FileResponse(zpath, filename=zpath.name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


# ================= AI assistant =================
def _conv_out(c):
    scope = json.loads(c["scope"]) if c.get("scope") else None
    return {"id": c["id"], "title": c["title"], "created_at": c["created_at"], "updated_at": c["updated_at"],
            "scope": agent.scope_label(scope)}


@app.get("/api/ai/conversations")
def ai_conversations(q: str = "", user=Depends(ai_user)):
    sql, args = "SELECT * FROM conversations WHERE user_id=?", [user["id"]]
    if q.strip():
        like = f"%{q.strip().lower()}%"
        sql += " AND (lower(coalesce(title,'')) LIKE ? OR id IN (SELECT conversation_id FROM messages WHERE lower(content) LIKE ?))"
        args += [like, like]
    return [_conv_out(c) for c in db.q(sql + " ORDER BY updated_at DESC LIMIT 300", args)]


@app.post("/api/ai/conversations")
def ai_new_conversation(body: dict = Body(default={}), user=Depends(ai_user)):
    ids = [int(x) for x in (body or {}).get("contract_ids") or []]
    for cid in ids:
        access.require_view(user, cid)
    cid = db.ex("INSERT INTO conversations (user_id, title, scope, created_at, updated_at) VALUES (?,?,?,?,?)",
                (user["id"], "新对话", json.dumps(ids) if ids else None, db.now(), db.now()))
    return _conv_out(db.q("SELECT * FROM conversations WHERE id=?", (cid,), one=True))


@app.get("/api/ai/conversations/{conv_id}")
def ai_get_conversation(conv_id: int, user=Depends(ai_user)):
    conv = agent.own_conversation(user, conv_id)
    out = _conv_out(dict(conv, scope=json.dumps(conv["scope"]) if conv["scope"] else None))
    out["messages"] = agent.message_rows(conv_id)
    return out


@app.put("/api/ai/conversations/{conv_id}")
def ai_rename_conversation(conv_id: int, body: dict = Body(...), user=Depends(ai_user)):
    agent.own_conversation(user, conv_id)
    title = (body.get("title") or "").strip()[:80] or "新对话"
    db.ex("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
    return {"ok": True, "title": title}


@app.delete("/api/ai/conversations/{conv_id}")
def ai_delete_conversation(conv_id: int, user=Depends(ai_user)):
    agent.own_conversation(user, conv_id)
    db.ex("DELETE FROM conversations WHERE id=?", (conv_id,))  # its messages go with it
    return {"ok": True}


@app.post("/api/ai/conversations/{conv_id}/messages")
async def ai_ask(conv_id: int, request: Request, body: dict = Body(...), user=Depends(ai_user)):
    conv = agent.own_conversation(user, conv_id)
    question = (body.get("content") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空 / Empty question")
    if len(question) > 4000:
        raise HTTPException(400, "问题太长（最多 4000 字）/ Question too long")
    agent.check_quota(user)
    q, cancel = agent.run_in_thread(user, conv, question)

    async def events():
        try:
            while True:
                if await request.is_disconnected():
                    cancel.set()  # user pressed stop / closed the page: stop the model, keep what was written
                    break
                try:
                    ev = await asyncio.to_thread(q.get, True, 0.5)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if ev is None:
                    break
                yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
        except asyncio.CancelledError:
            cancel.set()
            raise

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/ai/usage")
def ai_usage(user=Depends(ai_user)):
    return {"used": agent.usage_for(user["id"]), "limits": agent.limits_for(user)}


@app.get("/api/ai/usage/all")
def ai_usage_all(user=Depends(admin)):
    day = agent.today()
    month = day[:7]
    rows = db.q("SELECT u.id, u.username, u.display_name, u.ai_daily_questions, u.ai_daily_tokens, "
                "coalesce((SELECT questions FROM ai_usage a WHERE a.user_id=u.id AND a.day=?),0) q_today, "
                "coalesce((SELECT tokens FROM ai_usage a WHERE a.user_id=u.id AND a.day=?),0) t_today, "
                "coalesce((SELECT sum(questions) FROM ai_usage a WHERE a.user_id=u.id AND substr(a.day,1,7)=?),0) q_month, "
                "coalesce((SELECT sum(tokens) FROM ai_usage a WHERE a.user_id=u.id AND substr(a.day,1,7)=?),0) t_month "
                "FROM users u ORDER BY t_month DESC, u.id", (day, day, month, month))
    for r in rows:
        r["limits"] = agent.limits_for(r)
    total = db.q("SELECT coalesce(sum(tokens),0) n FROM ai_usage WHERE day=?", (day,), one=True)["n"]
    return {"users": rows, "global_today": total, "global_limit": int(llm.load_config()["ai_global_daily_tokens"])}


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
