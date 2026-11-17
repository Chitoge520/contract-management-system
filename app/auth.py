"""Users, login sessions, roles and the audit log."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request

from . import db

SESSION_COOKIE = "cms_session"
SESSION_DAYS = 7
ROLES = ("admin", "editor", "viewer")
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    contract_id INTEGER,
    contract_label TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_log(contract_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


def init():
    with db._lock, db.connect() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(contracts)")}
        for col in ("uploaded_by", "updated_by", "confirmed_by"):
            if col not in cols:
                c.execute(f"ALTER TABLE contracts ADD COLUMN {col} TEXT")
        c.commit()


# ---------------- passwords ----------------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), 200_000).hex()
    return hmac.compare_digest(calc, h)


def validate_password(pw: str):
    if not pw or len(pw) < 6:
        raise HTTPException(400, "密码至少 6 位 / Password must be at least 6 characters")


# ---------------- users ----------------
def public_user(u):
    if not u:
        return None
    return {k: u[k] for k in ("id", "username", "display_name", "role", "active", "created_at", "last_login")}


def user_count():
    return db.q("SELECT count(*) n FROM users", one=True)["n"]


def create_user(username, password, role="viewer", display_name=None):
    username = (username or "").strip()
    if not username:
        raise HTTPException(400, "用户名不能为空 / Username required")
    if role not in ROLES:
        raise HTTPException(400, "Invalid role")
    validate_password(password)
    if db.q("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,), one=True):
        raise HTTPException(400, "用户名已存在 / Username already exists")
    uid = db.ex("INSERT INTO users (username, display_name, password_hash, role, active, created_at) VALUES (?,?,?,?,1,?)",
                (username, display_name or username, hash_password(password), role, db.now()))
    return db.q("SELECT * FROM users WHERE id=?", (uid,), one=True)


# ---------------- sessions ----------------
def new_session(user_id):
    token = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    db.ex("INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)", (token, user_id, db.now(), exp))
    db.ex("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    return token


def end_session(token):
    db.ex("DELETE FROM sessions WHERE token=?", (token,))


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.q("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                   "WHERE s.token=? AND s.expires_at>=? AND u.active=1", (token, db.now()), one=True)
        if row:
            return row
    raise HTTPException(401, "请先登录 / Please sign in")


def require(role):
    def dep(user=Depends(current_user)):
        if ROLE_RANK.get(user["role"], 0) < ROLE_RANK[role]:
            raise HTTPException(403, "没有权限 / Permission denied")
        return user
    return dep


# ---------------- audit ----------------
def contract_label(cid):
    r = db.q("SELECT contract_no, title, file_name FROM contracts WHERE id=?", (cid,), one=True)
    if not r:
        return None
    return r["contract_no"] or r["title"] or r["file_name"]


def log(user, action, contract_id=None, detail=None, label=None):
    db.ex("INSERT INTO audit_log (ts, user_id, username, action, contract_id, contract_label, detail) VALUES (?,?,?,?,?,?,?)",
          (db.now(), user["id"] if user else None,
           (user.get("display_name") or user.get("username")) if user else None,
           action, contract_id, label or (contract_label(contract_id) if contract_id else None),
           json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def _flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = []
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def _norm(v):
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s.replace(",", "")) if s.replace(",", "").replace(".", "", 1).lstrip("-").isdigit() else s
    except ValueError:
        return s


def diff(old, new, limit=300):
    """Field-level changes between two extraction dicts: [{path, old, new}]."""
    a, b = _flatten(old or {}), _flatten(new or {})
    changes = []
    for path in sorted(set(a) | set(b), key=lambda p: (p.split("[")[0], p)):
        if _norm(a.get(path)) != _norm(b.get(path)):
            changes.append({"path": path, "old": a.get(path), "new": b.get(path)})
            if len(changes) >= limit:
                break
    return changes
