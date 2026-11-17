"""SQLite storage. One file: data/contracts.db"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "contracts.db"

_lock = threading.RLock()

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_items_contract ON line_items(contract_id);
"""

SCALAR_FIELDS = [
    "contract_type", "contract_no", "title", "title_zh", "party_a", "party_b",
    "signing_date", "effective_date", "start_date", "end_date", "currency", "total_amount",
]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    DATA_DIR.mkdir(exist_ok=True)
    FILES_DIR.mkdir(exist_ok=True)
    with _lock, connect() as c:
        c.executescript(SCHEMA)


def q(sql, args=(), one=False):
    with _lock, connect() as c:
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with _lock, connect() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def set_status(cid, status, progress=None, error=None):
    ex("UPDATE contracts SET status=?, progress=?, error=?, updated_at=? WHERE id=?",
       (status, progress, error, now(), cid))


def to_float(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace(" ", "")
    for sym in ("CNY", "RMB", "USD", "EUR", "ILS", "NIS", "¥", "$", "€", "₪", "元"):
        s = s.replace(sym, "")
    try:
        return float(s)
    except ValueError:
        return None


def save_data(cid, data, model=None):
    """Persist the (possibly edited) extraction JSON and sync searchable columns + line items."""
    basic = data.get("basic", {}) or {}
    amount = data.get("amount", {}) or {}
    vals = {
        "contract_type": data.get("contract_type") or basic.get("contract_type"),
        "contract_no": basic.get("contract_no"),
        "title": basic.get("title"),
        "title_zh": basic.get("title_zh"),
        "party_a": basic.get("party_a"),
        "party_b": basic.get("party_b"),
        "signing_date": basic.get("signing_date"),
        "effective_date": basic.get("effective_date"),
        "start_date": basic.get("start_date"),
        "end_date": basic.get("end_date"),
        "currency": amount.get("currency"),
        "total_amount": to_float(amount.get("total_amount")),
    }
    sets = ", ".join(f"{k}=?" for k in vals)
    args = list(vals.values()) + [json.dumps(data, ensure_ascii=False), now()]
    sql = f"UPDATE contracts SET {sets}, data_json=?, updated_at=?"
    if model:
        sql += ", llm_model=?"
        args.append(model)
    sql += " WHERE id=?"
    args.append(cid)
    with _lock, connect() as c:
        c.execute(sql, args)
        c.execute("DELETE FROM line_items WHERE contract_id=?", (cid,))
        for i, it in enumerate(data.get("line_items") or []):
            c.execute(
                "INSERT INTO line_items (contract_id, seq, category, name, name_zh, description, quantity, unit,"
                " unit_price, amount, currency, page) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, i + 1, it.get("category"), it.get("name"), it.get("name_zh"), it.get("description"),
                 to_float(it.get("quantity")), it.get("unit"), to_float(it.get("unit_price")),
                 to_float(it.get("amount")), it.get("currency") or vals["currency"], it.get("page")))
        c.commit()


def delete_contract(cid):
    with _lock, connect() as c:
        c.execute("DELETE FROM line_items WHERE contract_id=?", (cid,))
        c.execute("DELETE FROM pages WHERE contract_id=?", (cid,))
        c.execute("DELETE FROM contracts WHERE id=?", (cid,))
        c.commit()
