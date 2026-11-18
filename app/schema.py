"""Database structure and its upgrades, all in one place.

The database file remembers its version (PRAGMA user_version). At start-up every step newer than that
version runs once, in order, and the version is moved forward. Before upgrading an existing database a copy
is saved in data/backups/.

To change the structure later: add a function at the end of MIGRATIONS. Never edit a step that has shipped.
"""
import json
import shutil
import sqlite3
from datetime import datetime

from . import db

# ---------------------------------------------------------------- step 1
# The tables as they were before versioning existed. Safe to run on an empty or an older database.
BASELINE = """
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    file_hash TEXT,
    uploaded_at TEXT,
    updated_at TEXT,
    status TEXT DEFAULT 'queued',      -- queued / reading / extracting / review / confirmed / failed
    progress TEXT,
    error TEXT,
    page_count INTEGER DEFAULT 0,
    ocr_pages INTEGER DEFAULT 0,
    redaction_count INTEGER DEFAULT 0,
    contract_type TEXT,
    contract_no TEXT,
    title TEXT,
    title_zh TEXT,
    party_a TEXT,
    party_b TEXT,
    signing_date TEXT,
    effective_date TEXT,
    start_date TEXT,
    end_date TEXT,
    currency TEXT,
    total_amount REAL,
    lifecycle_status TEXT DEFAULT 'active', -- draft / active / completed / terminated
    notes TEXT,
    data_json TEXT,
    llm_model TEXT,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    contract_id INTEGER,
    page_no INTEGER,
    text TEXT,
    redacted_text TEXT,
    is_ocr INTEGER DEFAULT 0,
    PRIMARY KEY (contract_id, page_no)
);
CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER,
    seq INTEGER,
    category TEXT,
    name TEXT,
    name_zh TEXT,
    description TEXT,
    quantity REAL,
    unit TEXT,
    unit_price REAL,
    amount REAL,
    currency TEXT,
    page INTEGER
);
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
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT,
    scope TEXT,                 -- JSON list of contract ids, NULL = all contracts the user may see
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,         -- user / assistant
    content TEXT,
    steps TEXT,                 -- JSON: tools the assistant used
    citations TEXT,             -- JSON: contracts cited
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    model TEXT,
    status TEXT DEFAULT 'done', -- done / stopped / error
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    questions INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""
# columns that older versions of the program added one by one
BASELINE_COLUMNS = [
    ("contracts", "uploaded_by", "TEXT"), ("contracts", "updated_by", "TEXT"), ("contracts", "confirmed_by", "TEXT"),
    ("users", "status", "TEXT NOT NULL DEFAULT 'active'"),  # active / pending (self-registered, waiting for an admin)
    ("users", "employee_no", "TEXT"),
    ("users", "department_id", "INTEGER"), ("users", "position", "TEXT"),
    ("users", "ai_daily_questions", "INTEGER"), ("users", "ai_daily_tokens", "INTEGER"),  # NULL = default quota
    ("sessions", "last_seen", "TEXT"),
]


def _columns(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def step_baseline(c):
    c.executescript("BEGIN;" + BASELINE + "COMMIT;")
    for table, col, decl in BASELINE_COLUMNS:
        if col not in _columns(c, table):
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_employee_no ON users(employee_no COLLATE NOCASE) "
              "WHERE employee_no IS NOT NULL")


# ---------------------------------------------------------------- step 2
# Foreign keys: rows that belong to a contract, user or conversation are removed together with it,
# and can never point to something that does not exist. SQLite can only add them by rebuilding the table.
# Rows that already point to nothing (left behind by older versions) are dropped during the copy.
# The audit log deliberately has no foreign keys: it must survive deletions.
REBUILDS = [
    ("pages", """CREATE TABLE pages (
        contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        page_no INTEGER NOT NULL,
        text TEXT,
        redacted_text TEXT,
        is_ocr INTEGER DEFAULT 0,
        PRIMARY KEY (contract_id, page_no))""",
     "contract_id IN (SELECT id FROM contracts)", []),
    ("line_items", """CREATE TABLE line_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        seq INTEGER, category TEXT, name TEXT, name_zh TEXT, description TEXT,
        quantity REAL, unit TEXT, unit_price REAL, amount REAL, currency TEXT, page INTEGER)""",
     "contract_id IN (SELECT id FROM contracts)",
     ["CREATE INDEX idx_items_contract ON line_items(contract_id)"]),
    ("contract_departments", """CREATE TABLE contract_departments (
        contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
        PRIMARY KEY (contract_id, department_id))""",
     "contract_id IN (SELECT id FROM contracts) AND department_id IN (SELECT id FROM departments)",
     ["CREATE INDEX idx_cd_dept ON contract_departments(department_id)"]),
    ("sessions", """CREATE TABLE sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT, expires_at TEXT, last_seen TEXT)""",
     "user_id IN (SELECT id FROM users)",
     ["CREATE INDEX idx_sessions_user ON sessions(user_id)"]),
    ("conversations", """CREATE TABLE conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT,
        scope TEXT,
        created_at TEXT, updated_at TEXT)""",
     "user_id IN (SELECT id FROM users)",
     ["CREATE INDEX idx_conv_user ON conversations(user_id, updated_at)"]),
    ("messages", """CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL, content TEXT, steps TEXT, citations TEXT,
        tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, model TEXT,
        status TEXT DEFAULT 'done', created_at TEXT)""",
     "conversation_id IN (SELECT id FROM conversations)",
     ["CREATE INDEX idx_msg_conv ON messages(conversation_id, id)"]),
    ("ai_usage", """CREATE TABLE ai_usage (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        questions INTEGER DEFAULT 0,
        tokens INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day))""",
     "user_id IN (SELECT id FROM users)", []),
]


def step_foreign_keys(c):
    for table, create, keep, indexes in REBUILDS:
        c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        c.execute(create)
        cols = ", ".join(sorted(_columns(c, table) & _columns(c, f"{table}_old")))
        c.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old WHERE {keep}")
        c.execute(f"DROP TABLE {table}_old")
        for sql in indexes:
            c.execute(sql)


# ---------------------------------------------------------------- step 3
# A search column with the contract's content only. Searching the raw JSON also matched its field names,
# so English words such as "warranty", "amount" or "page" found every contract.
def step_search_text(c):
    if "search_text" not in _columns(c, "contracts"):
        c.execute("ALTER TABLE contracts ADD COLUMN search_text TEXT")
    rows = c.execute("SELECT * FROM contracts").fetchall()
    for r in rows:
        c.execute("UPDATE contracts SET search_text=? WHERE id=?", (db.search_text_for(dict(r)), r["id"]))


# ---------------------------------------------------------------- step 4
# Payment nodes with the record of whether they were paid, and proof files (see payments.py).
# Nodes are created for contracts that were already analysed; the editor role may record payments.
PAYMENT_TABLES = """
CREATE TABLE payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    seq INTEGER,
    trigger_en TEXT,            -- payment condition, contract language
    trigger_zh TEXT,
    percentage REAL,
    amount REAL,                -- planned amount
    currency TEXT,
    due_rule TEXT,              -- e.g. "within 30 days after acceptance"
    due_date TEXT,              -- planned date set by a user (YYYY-MM-DD)
    page INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid')),
    paid_amount REAL,
    paid_date TEXT,
    note TEXT,
    recorded_by TEXT,
    recorded_at TEXT,
    source TEXT NOT NULL DEFAULT 'ai' CHECK (source IN ('ai', 'manual')),
    created_at TEXT
);
CREATE INDEX idx_payments_contract ON payments(contract_id, seq);
CREATE INDEX idx_payments_status ON payments(status, due_date);
CREATE TABLE payment_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER,
    uploaded_by TEXT,
    uploaded_at TEXT
);
CREATE INDEX idx_payment_files_payment ON payment_files(payment_id);
"""


def step_payments(c):
    from . import payments
    for sql in PAYMENT_TABLES.split(";"):
        if sql.strip():
            c.execute(sql)
    for r in c.execute("SELECT id, data_json FROM contracts WHERE data_json IS NOT NULL").fetchall():
        try:
            data = json.loads(r["data_json"])
        except ValueError:
            continue
        payments.sync_from_extraction(r["id"], data, conn=c)
    row = c.execute("SELECT permissions FROM roles WHERE key='editor'").fetchone()
    if row:
        perms = json.loads(row["permissions"] or "[]")
        if "payment.record" not in perms:
            c.execute("UPDATE roles SET permissions=? WHERE key='editor'", (json.dumps(perms + ["payment.record"]),))


# ---------------------------------------------------------------- step 5
# Projects: a contract can belong to one project. Deleting a project leaves its contracts without a project.
def step_projects(c):
    c.execute("""CREATE TABLE projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT,                  -- optional project number
        note TEXT,
        created_at TEXT,
        created_by TEXT)""")
    c.execute("CREATE UNIQUE INDEX idx_projects_name ON projects(name COLLATE NOCASE)")
    if "project_id" not in _columns(c, "contracts"):
        c.execute("ALTER TABLE contracts ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_contracts_project ON contracts(project_id)")
    row = c.execute("SELECT permissions FROM roles WHERE key='editor'").fetchone()
    if row:
        perms = json.loads(row["permissions"] or "[]")
        if "project.manage" not in perms:
            c.execute("UPDATE roles SET permissions=? WHERE key='editor'", (json.dumps(perms + ["project.manage"]),))


# ---------------------------------------------------------------- step 6
# Module management: modules become role permissions (every existing role keeps what it had),
# and a person can have modules switched off individually.
def step_modules(c):
    if "modules_off" not in _columns(c, "users"):
        c.execute("ALTER TABLE users ADD COLUMN modules_off TEXT")   # JSON list of module keys, NULL = none
    for r in c.execute("SELECT key, permissions FROM roles").fetchall():
        perms = json.loads(r["permissions"] or "[]")
        add = [p for p in ("module.projects", "module.payments") if p not in perms]
        if add:
            c.execute("UPDATE roles SET permissions=? WHERE key=?", (json.dumps(add + perms), r["key"]))


MIGRATIONS = [step_baseline, step_foreign_keys, step_search_text, step_payments, step_projects, step_modules]
LATEST = len(MIGRATIONS)


def version(c):
    return c.execute("PRAGMA user_version").fetchone()[0]


def _backup_before_upgrade(c, current):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contracts'").fetchone():
        return None  # brand-new database: nothing to keep
    folder = db.DATA_DIR / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / f"contracts_v{current}_before_upgrade_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst = sqlite3.connect(path)
    c.backup(dst)
    dst.close()
    return path


def run():
    """Bring the database up to the latest version. Each step runs in its own transaction: if a step fails,
    nothing of it is kept, the error is raised and the program does not start on a half-upgraded database."""
    db.DATA_DIR.mkdir(exist_ok=True)
    db.FILES_DIR.mkdir(exist_ok=True)
    with db._lock:
        c = sqlite3.connect(db.DB_PATH, timeout=30, isolation_level=None)  # we manage transactions ourselves
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            current = version(c)
            if current >= LATEST:
                return
            backup = _backup_before_upgrade(c, current)
            if backup:
                print(f"数据库升级前已备份 / Database backed up before upgrade: {backup}")
            c.execute("PRAGMA foreign_keys=OFF")  # cannot change inside a transaction; needed while rebuilding
            for n in range(current, LATEST):
                step = MIGRATIONS[n]
                if step is step_baseline:  # executescript manages its own transaction
                    step(c)
                    c.execute(f"PRAGMA user_version={n + 1}")
                    continue
                c.execute("BEGIN IMMEDIATE")
                try:
                    step(c)
                    broken = c.execute("PRAGMA foreign_key_check").fetchall()
                    if broken:
                        raise RuntimeError(f"foreign key check failed after step {n + 1}: {[tuple(b) for b in broken][:5]}")
                    c.execute(f"PRAGMA user_version={n + 1}")
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            c.execute("PRAGMA foreign_keys=ON")
        finally:
            c.close()
