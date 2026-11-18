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

# Table definitions and upgrades: see schema.py

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
    """Create the database or upgrade it to the current structure (see schema.py)."""
    from . import schema
    schema.run()


def _values(obj, out):
    """Every text / number inside the extraction JSON, without the field names."""
    if isinstance(obj, dict):
        for v in obj.values():
            _values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _values(v, out)
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, float) and obj.is_integer():
        out.append(str(int(obj)))
    else:
        out.append(str(obj))
    return out


def search_text_for(row):
    """What the contract list and the AI assistant search in: file name, number, titles, parties, notes and the
    content of the AI extraction (not its JSON field names). Lower-cased; queries are lower-cased the same way."""
    parts = [row.get(k) for k in ("file_name", "contract_no", "title", "title_zh", "party_a", "party_b", "notes")]
    try:
        data = json.loads(row["data_json"]) if row.get("data_json") else None
    except (TypeError, ValueError):
        data = None
    parts += _values(data, []) if data is not None else []
    return "\n".join(str(p) for p in parts if p not in (None, "")).lower()


def refresh_search(cid, conn=None):
    """Recompute the search column of one contract (after its data, notes or file name changed)."""
    def work(c):
        r = c.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
        if r:
            c.execute("UPDATE contracts SET search_text=? WHERE id=?", (search_text_for(dict(r)), cid))
    if conn is not None:
        return work(conn)
    with _lock, connect() as c:
        work(c)
        c.commit()


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
        refresh_search(cid, c)
        c.commit()


def delete_contract(cid):
    """Pages, line items and department links go with it (foreign keys, see schema.py)."""
    ex("DELETE FROM contracts WHERE id=?", (cid,))
