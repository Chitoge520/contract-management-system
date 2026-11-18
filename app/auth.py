"""Users, login sessions, roles and the audit log."""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request

from . import db

SESSION_COOKIE = "cms_session"
SESSION_HOURS = 12      # a login never lasts longer than this
IDLE_MINUTES = 30       # signed out after this long without any request (no page of the system open)
# Every action an administrator can grant or withhold per role.
PERMISSIONS = [
    "contract.upload",       # upload contracts
    "contract.edit",         # edit extracted data, notes, contract status
    "contract.review",       # confirm / undo review
    "contract.reprocess",    # re-read text / re-run AI extraction
    "contract.assign_dept",  # change which departments a contract belongs to
    "contract.delete",       # delete contracts
    "contract.download",     # download the original file
    "contract.export",       # export to Excel
    "ai.use",                # use the AI assistant
    "audit.view",            # view the audit log
]
BUILTIN_ROLES = {
    "admin": ("管理员", PERMISSIONS),
    "editor": ("编辑", ["contract.upload", "contract.edit", "contract.review", "contract.reprocess",
                        "contract.assign_dept", "contract.download", "contract.export", "ai.use", "audit.view"]),
    "viewer": ("只读", ["contract.download", "contract.export", "ai.use", "audit.view"]),
}

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
CREATE TABLE IF NOT EXISTS roles (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT '[]',
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
"""


def init():
    with db._lock, db.connect() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(contracts)")}
        for col in ("uploaded_by", "updated_by", "confirmed_by"):
            if col not in cols:
                c.execute(f"ALTER TABLE contracts ADD COLUMN {col} TEXT")
        ucols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "status" not in ucols:      # active / pending (self-registered, waiting for an admin)
            c.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "employee_no" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN employee_no TEXT")
        if "last_seen" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:
            c.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_employee_no ON users(employee_no COLLATE NOCASE) "
                  "WHERE employee_no IS NOT NULL")
        for key, (name, perms) in BUILTIN_ROLES.items():
            if not c.execute("SELECT 1 FROM roles WHERE key=?", (key,)).fetchone():
                c.execute("INSERT INTO roles (key, name, permissions, builtin, created_at) VALUES (?,?,?,1,?)",
                          (key, name, json.dumps(perms), db.now()))
        c.commit()


# ---------------- roles & permissions ----------------
def role(key):
    r = db.q("SELECT * FROM roles WHERE key=?", (key,), one=True)
    if r:
        r["permissions"] = PERMISSIONS if key == "admin" else [p for p in json.loads(r["permissions"]) if p in PERMISSIONS]
    return r


def roles():
    out = db.q("SELECT * FROM roles ORDER BY builtin DESC, created_at")
    for r in out:
        r["permissions"] = PERMISSIONS if r["key"] == "admin" else [p for p in json.loads(r["permissions"]) if p in PERMISSIONS]
        r["users"] = db.q("SELECT count(*) n FROM users WHERE role=?", (r["key"],), one=True)["n"]
    return out


def perms_of(user):
    if user.get("role") == "admin":
        return list(PERMISSIONS)
    r = role(user.get("role"))
    return r["permissions"] if r else []


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
    import re
    if not pw or len(pw) < 10 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise HTTPException(400, "密码至少 10 位，且需同时包含字母和数字 / "
                                 "Password needs at least 10 characters with both letters and digits")


# ---------------- field rules ----------------
import re as _re
USERNAME_RE = _re.compile(r"^[A-Za-z0-9]{3,32}$")
EMPLOYEE_NO_RE = _re.compile(r"^[A-Za-z0-9-]{1,32}$")


def validate_username(name: str):
    if not USERNAME_RE.match(name or ""):
        raise HTTPException(400, "用户名只能包含英文字母和数字，长度 3-32 位，不能有空格或特殊字符 / "
                                 "Username: 3-32 letters or digits only, no spaces or symbols")
    if db.q("SELECT 1 FROM users WHERE lower(username)=lower(?)", (name,), one=True):
        raise HTTPException(400, "用户名已被使用 / Username already taken")


def normalize_employee_no(no):
    return (no or "").strip()


def validate_employee_no(no: str, exclude_user_id=None):
    if not EMPLOYEE_NO_RE.match(no or ""):
        raise HTTPException(400, "工号必填，只能包含字母、数字和“-”，最长 32 位 / "
                                 "Employee ID is required: letters, digits and '-' only, max 32")
    row = db.q("SELECT id FROM users WHERE lower(employee_no)=lower(?)", (no,), one=True)
    if row and row["id"] != exclude_user_id:
        raise HTTPException(400, "该工号已存在 / This employee ID already exists")


# ---------------- users ----------------
def public_user(u):
    if not u:
        return None
    out = {k: u.get(k) for k in ("id", "username", "display_name", "role", "active", "created_at", "last_login",
                                  "department_id", "position", "ai_daily_questions", "ai_daily_tokens",
                                  "employee_no", "status")}
    dept = db.q("SELECT name, see_all FROM departments WHERE id=?", (u.get("department_id"),), one=True) \
        if u.get("department_id") else None
    out["department_name"] = dept["name"] if dept else None
    r = role(u.get("role"))
    out["role_name"] = r["name"] if r else u.get("role")
    out["permissions"] = perms_of(u)
    out["sees_all"] = u.get("role") == "admin" or bool(dept and dept["see_all"])
    return out


def user_count():
    return db.q("SELECT count(*) n FROM users", one=True)["n"]


def create_user(username, password, role="viewer", display_name=None, employee_no=None, status="active"):
    username = (username or "").strip()
    validate_username(username)
    employee_no = normalize_employee_no(employee_no)
    validate_employee_no(employee_no)
    if not db.q("SELECT 1 FROM roles WHERE key=?", (role,), one=True):
        raise HTTPException(400, "Invalid role")
    validate_password(password)
    uid = db.ex("INSERT INTO users (username, display_name, password_hash, role, active, created_at, employee_no, status) "
                "VALUES (?,?,?,?,1,?,?,?)",
                (username, (display_name or "").strip() or username, hash_password(password), role, db.now(), employee_no, status))
    return db.q("SELECT * FROM users WHERE id=?", (uid,), one=True)


# ---------------- sessions ----------------
def new_session(user_id):
    token = secrets.token_urlsafe(32)
    exp = _ts(datetime.now() + timedelta(hours=SESSION_HOURS))
    db.ex("INSERT INTO sessions (token, user_id, created_at, expires_at, last_seen) VALUES (?,?,?,?,?)",
          (token, user_id, db.now(), exp, db.now()))
    db.ex("DELETE FROM sessions WHERE expires_at < ? OR coalesce(last_seen, created_at) < ?", (db.now(), _idle_cutoff()))
    return token


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _idle_cutoff():
    return _ts(datetime.now() - timedelta(minutes=IDLE_MINUTES))


def end_session(token):
    db.ex("DELETE FROM sessions WHERE token=?", (token,))


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.q("SELECT u.*, s.last_seen AS session_last_seen FROM sessions s JOIN users u ON u.id=s.user_id "
                   "WHERE s.token=? AND s.expires_at>=? AND coalesce(s.last_seen, s.created_at)>=? "
                   "AND u.active=1 AND u.status='active'", (token, db.now(), _idle_cutoff()), one=True)
        if row:
            # an open page keeps polling, which keeps the login alive; write at most once a minute
            if (row.pop("session_last_seen") or "") < _ts(datetime.now() - timedelta(seconds=60)):
                db.ex("UPDATE sessions SET last_seen=? WHERE token=?", (db.now(), token))
            return row
    raise HTTPException(401, "请先登录 / Please sign in")


def require(perm):
    """Dependency: 'admin' = administrators only; otherwise a permission from PERMISSIONS."""
    def dep(user=Depends(current_user)):
        ok = user["role"] == "admin" if perm == "admin" else perm in perms_of(user)
        if not ok:
            raise HTTPException(403, "没有权限，请联系管理员 / Permission denied")
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
