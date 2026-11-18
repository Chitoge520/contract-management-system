"""Departments, positions and who may see which contract.

Rules
- Admins, and members of a department marked "see all contracts", see every contract.
- A contract assigned to one or more departments is visible to members of those departments.
- A contract with no department is company-wide: everyone can see it.
- New uploads default to the uploader's department.
"""
from fastapi import HTTPException

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    see_all INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS contract_departments (
    contract_id INTEGER NOT NULL,
    department_id INTEGER NOT NULL,
    PRIMARY KEY (contract_id, department_id)
);
CREATE INDEX IF NOT EXISTS idx_cd_dept ON contract_departments(department_id);
"""


def init():
    with db._lock, db.connect() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "department_id" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN department_id INTEGER")
        if "position" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN position TEXT")
        for col in ("ai_daily_questions", "ai_daily_tokens"):  # per-user AI quota overrides (NULL = default)
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER")
        # links left behind by contracts deleted before delete_contract cleaned them up
        c.execute("DELETE FROM contract_departments WHERE contract_id NOT IN (SELECT id FROM contracts)")
        c.commit()


def department(dept_id):
    if not dept_id:
        return None
    return db.q("SELECT * FROM departments WHERE id=?", (dept_id,), one=True)


def sees_all(user) -> bool:
    if user["role"] == "admin":
        return True
    d = department(user.get("department_id"))
    return bool(d and d["see_all"])


def visible_sql(user, alias="contracts"):
    """SQL condition (and args) limiting a query on the contracts table to what this user may see."""
    if sees_all(user):
        return "1=1", []
    cond = (f"(NOT EXISTS (SELECT 1 FROM contract_departments cd WHERE cd.contract_id={alias}.id)"
            f" OR EXISTS (SELECT 1 FROM contract_departments cd WHERE cd.contract_id={alias}.id AND cd.department_id=?))")
    return cond, [user.get("department_id") or -1]


def can_view(user, cid) -> bool:
    cond, args = visible_sql(user, "c")
    return bool(db.q(f"SELECT 1 FROM contracts c WHERE c.id=? AND {cond}", [cid] + args, one=True))


def visible_ids(user):
    cond, args = visible_sql(user, "c")
    return [r["id"] for r in db.q(f"SELECT c.id FROM contracts c WHERE {cond}", args)]


def departments_of(cid):
    return db.q("SELECT d.id, d.name FROM contract_departments cd JOIN departments d ON d.id=cd.department_id "
                "WHERE cd.contract_id=? ORDER BY d.name", (cid,))


def departments_map(ids):
    """{contract_id: [{"id","name"}...]} for many contracts at once."""
    out = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in db.q(f"SELECT cd.contract_id cid, d.id, d.name FROM contract_departments cd "
                  f"JOIN departments d ON d.id=cd.department_id WHERE cd.contract_id IN ({marks}) ORDER BY d.name", ids):
        out[r["cid"]].append({"id": r["id"], "name": r["name"]})
    return out


def set_departments(cid, dept_ids):
    dept_ids = sorted({int(x) for x in dept_ids or []})
    for d in dept_ids:
        if not department(d):
            raise HTTPException(400, f"Department {d} not found")
    with db._lock, db.connect() as c:
        c.execute("DELETE FROM contract_departments WHERE contract_id=?", (cid,))
        for d in dept_ids:
            c.execute("INSERT INTO contract_departments (contract_id, department_id) VALUES (?,?)", (cid, d))
        c.commit()
    return dept_ids


def assign_default(cid, user):
    """New uploads belong to the uploader's department (company-wide if the uploader has none)."""
    if user.get("department_id") and department(user["department_id"]):
        set_departments(cid, [user["department_id"]])


def require_view(user, cid):
    if not can_view(user, cid):
        raise HTTPException(404, "Contract not found")
