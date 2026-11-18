"""Payment nodes of a contract and the record of whether each one has been paid, with proof files.

Nodes are created from the payment terms the AI extracted. Users record payment on them (amount, date, note)
and attach proof such as bank receipts. A node that someone has recorded anything on is never changed or
removed by a later AI run; nodes nobody has touched are refreshed from the new extraction.
"""
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from . import db

PROOF_DIR_NAME = "payments"                 # inside data/files
PROOF_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".docx": None, ".xlsx": None}
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}   # shown in the browser; others download
MAX_PROOF_BYTES = 20 * 1024 * 1024


def proof_dir() -> Path:
    d = db.FILES_DIR / PROOF_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ reading
def _files_for(ids):
    out = {i: [] for i in ids}
    if ids:
        marks = ",".join("?" * len(ids))
        for f in db.q(f"SELECT id, payment_id, file_name, size, uploaded_by, uploaded_at FROM payment_files "
                      f"WHERE payment_id IN ({marks}) ORDER BY id", list(ids)):
            out[f["payment_id"]].append(f)
    return out


def for_contract(cid):
    rows = db.q("SELECT * FROM payments WHERE contract_id=? ORDER BY seq, id", (cid,))
    files = _files_for([r["id"] for r in rows])
    day = today()
    for r in rows:
        r["files"] = files[r["id"]]
        r["state"] = state(r, day)
    return rows


def summary(cid):
    return summaries([cid]).get(cid)


def paid_sql(a="payments"):
    """A node counts as paid only with a complete record: amount paid > 0, payment date and currency."""
    return (f"({a}.status='paid' AND {a}.paid_amount>0 AND coalesce({a}.paid_date,'')<>'' "
            f"AND coalesce({a}.currency,'')<>'')")


def is_paid(p):
    return p.get("status") == "paid" and (p.get("paid_amount") or 0) > 0 and bool(p.get("paid_date")) \
        and bool(p.get("currency"))


def state(p, day=None):
    """paid / incomplete (marked paid, details missing) / overdue / pending"""
    if is_paid(p):
        return "paid"
    if p.get("status") == "paid":
        return "incomplete"
    if p.get("due_date") and p["due_date"] < (day or today()):
        return "overdue"
    return "pending"


def summaries(cids):
    """{contract_id: {"total", "paid", "incomplete", "overdue"}} for the ledger's progress column."""
    if not cids:
        return {}
    marks = ",".join("?" * len(cids))
    ok = paid_sql()
    rows = db.q(f"SELECT contract_id, count(*) total, sum({ok}) paid, sum(status='paid' AND NOT {ok}) incomplete, "
                f"sum(NOT {ok} AND status<>'paid' AND due_date IS NOT NULL AND due_date<?) overdue "
                f"FROM payments WHERE contract_id IN ({marks}) GROUP BY contract_id", [today()] + list(cids))
    return {r["contract_id"]: {"total": r["total"], "paid": r["paid"] or 0, "incomplete": r["incomplete"] or 0,
                               "overdue": r["overdue"] or 0} for r in rows}


def get(pid):
    p = db.q("SELECT * FROM payments WHERE id=?", (pid,), one=True)
    if not p:
        raise HTTPException(404, "Payment not found")
    return p


def today():
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------ from the AI extraction
def _touched(c, p):
    return p["status"] == "paid" or p["source"] == "manual" or p.get("note") or p.get("due_date") \
        or p.get("paid_amount") is not None or p.get("paid_date") \
        or c.execute("SELECT 1 FROM payment_files WHERE payment_id=?", (p["id"],)).fetchone()


def sync_from_extraction(cid, data, conn=None):
    """Create / refresh the AI nodes of a contract from data["payment_terms"]. Recorded nodes are kept as they are."""
    if conn is not None:
        return _sync(conn, cid, data)
    with db._lock, db.connect() as c:
        _sync(c, cid, data)
        c.commit()


def _sync(c, cid, data):
    data = data or {}
    terms = [t for t in (data.get("payment_terms") or []) if isinstance(t, dict)]
    total = db.to_float((data.get("amount") or {}).get("total_amount"))
    currency = (data.get("amount") or {}).get("currency")
    existing = [dict(r) for r in c.execute("SELECT * FROM payments WHERE contract_id=?", (cid,))]
    kept_seqs = set()
    for p in existing:
        if p["source"] == "ai" and not _touched(c, p):
            c.execute("DELETE FROM payments WHERE id=?", (p["id"],))
        else:
            kept_seqs.add(p["seq"])
    for i, t in enumerate(terms):
        seq = t.get("seq") if isinstance(t.get("seq"), int) else i + 1
        if seq in kept_seqs:
            continue  # someone already recorded this node; keep their version
        pct = db.to_float(t.get("percentage"))
        amount = db.to_float(t.get("amount"))
        if amount is None and pct is not None and total is not None:
            amount = round(total * pct / 100, 2)
        c.execute("INSERT INTO payments (contract_id, seq, trigger_en, trigger_zh, percentage, amount, currency, due_rule, "
                  "page, status, source, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', 'ai', ?)",
                  (cid, seq, t.get("trigger"), t.get("trigger_zh"), pct, amount, currency, t.get("due_rule"),
                   t.get("page") if isinstance(t.get("page"), int) else None, db.now()))


# ------------------------------------------------------------ changes by users
EDITABLE = ("seq", "trigger_en", "trigger_zh", "percentage", "amount", "currency", "due_rule", "due_date",
            "status", "paid_amount", "paid_date", "note")


def _clean(body):
    out = {}
    for k in EDITABLE:
        if k not in body:
            continue
        v = body[k]
        if k in ("percentage", "amount", "paid_amount"):
            if v not in (None, ""):
                f = db.to_float(v)
                if f is None or f != f:
                    raise HTTPException(400, "金额和比例只能填数字 / Amounts and percentages must be numbers")
                if f < 0:
                    raise HTTPException(400, "金额和比例不能是负数 / Amounts and percentages cannot be negative")
                if k == "percentage" and f > 100:
                    raise HTTPException(400, "比例不能超过 100% / Percentage cannot exceed 100")
                v = f
            else:
                v = None
        elif k == "seq":
            try:
                v = int(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                raise HTTPException(400, "期次只能填整数 / Instalment number must be a whole number")
            if v is not None and v < 1:
                raise HTTPException(400, "期次至少为 1 / Instalment number must be at least 1")
        elif k == "currency":
            v = (str(v or "").strip().upper()) or None
            if v and not (len(v) == 3 and v.isascii() and v.isalpha()):
                raise HTTPException(400, "币种请用 3 个英文字母的代码，例如 USD / Currency must be a 3-letter code")
        elif k in ("due_date", "paid_date"):
            v = (v or "").strip()[:10] or None
            if v:
                try:
                    datetime.strptime(v, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(400, "日期格式应为 YYYY-MM-DD / Date must be YYYY-MM-DD")
        elif k == "status":
            if v not in ("pending", "paid"):
                raise HTTPException(400, "Invalid status")
        else:
            v = (str(v).strip() if v is not None else "") or None
        out[k] = v
    return out


def _check_paid(p):
    """A payment marked as paid needs the amount paid (> 0), the payment date (not in the future) and the currency."""
    if p.get("status") != "paid":
        return
    missing = []
    if p.get("paid_amount") is None:
        missing.append("实付金额 amount paid")
    if not p.get("paid_date"):
        missing.append("付款日期 payment date")
    if not p.get("currency"):
        missing.append("币种 currency")
    if missing:
        raise HTTPException(400, "登记为已付款需要填写：" + "、".join(m.split(" ")[0] for m in missing)
                            + " / To mark as paid, fill in: " + ", ".join(m.split(" ", 1)[1] for m in missing))
    if p["paid_amount"] <= 0:
        raise HTTPException(400, "实付金额必须大于 0 / Amount paid must be greater than 0")
    if p["paid_date"] > today():
        raise HTTPException(400, "付款日期不能晚于今天 / Payment date cannot be in the future")


def create(cid, body, user_name):
    vals = _clean(body)
    if not vals.get("currency"):
        vals["currency"] = (db.q("SELECT currency FROM contracts WHERE id=?", (cid,), one=True) or {}).get("currency")
    _check_paid(vals)
    if vals.get("seq") is None:
        vals["seq"] = (db.q("SELECT max(seq) n FROM payments WHERE contract_id=?", (cid,), one=True)["n"] or 0) + 1
    vals.setdefault("status", "pending")
    if vals["status"] == "paid":
        vals.update(recorded_by=user_name, recorded_at=db.now())
    cols = list(vals) + ["contract_id", "source", "created_at"]
    pid = db.ex(f"INSERT INTO payments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                list(vals.values()) + [cid, "manual", db.now()])
    return get(pid)


def update(pid, body, user_name):
    """Returns (new row, {field: [old, new]})."""
    p = get(pid)
    vals = _clean(body)
    changes = {k: [p.get(k), v] for k, v in vals.items() if (p.get(k) if p.get(k) != "" else None) != v}
    if not changes:
        return p, {}
    if vals.get("status") == "pending" and p["status"] == "paid":
        vals.update(paid_amount=None, paid_date=None)  # marked as not paid any more
    _check_paid({**p, **vals})
    vals.update(recorded_by=user_name, recorded_at=db.now())
    db.ex(f"UPDATE payments SET {', '.join(k + '=?' for k in vals)} WHERE id=?", list(vals.values()) + [pid])
    return get(pid), changes


def delete(pid):
    p = get(pid)
    for f in db.q("SELECT stored_name FROM payment_files WHERE payment_id=?", (pid,)):
        (proof_dir() / f["stored_name"]).unlink(missing_ok=True)
    db.ex("DELETE FROM payments WHERE id=?", (pid,))  # its file records go with it
    return p


def delete_contract_files(cid):
    """Proof files on disk of a contract that is being deleted (the rows go with the contract)."""
    for f in db.q("SELECT f.stored_name FROM payment_files f JOIN payments p ON p.id=f.payment_id WHERE p.contract_id=?",
                  (cid,)):
        (proof_dir() / f["stored_name"]).unlink(missing_ok=True)


# ------------------------------------------------------------ proof files
def add_file(pid, file_name, content, user_name):
    ext = Path(file_name or "").suffix.lower()
    if ext not in PROOF_TYPES:
        raise HTTPException(400, "只能上传 PDF、图片（PNG / JPG / WEBP）、Word 或 Excel 文件 / "
                                 "Only PDF, images (PNG/JPG/WEBP), Word or Excel files")
    if not content:
        raise HTTPException(400, "文件是空的 / Empty file")
    if len(content) > MAX_PROOF_BYTES:
        raise HTTPException(400, "文件不能超过 20 MB / File larger than 20 MB")
    stored = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(6)}{ext}"
    (proof_dir() / stored).write_bytes(content)
    fid = db.ex("INSERT INTO payment_files (payment_id, file_name, stored_name, size, uploaded_by, uploaded_at) "
                "VALUES (?,?,?,?,?,?)", (pid, Path(file_name).name[:200], stored, len(content), user_name, db.now()))
    return db.q("SELECT id, payment_id, file_name, size, uploaded_by, uploaded_at FROM payment_files WHERE id=?",
                (fid,), one=True)


def get_file(fid):
    f = db.q("SELECT f.*, p.contract_id FROM payment_files f JOIN payments p ON p.id=f.payment_id WHERE f.id=?",
             (fid,), one=True)
    if not f:
        raise HTTPException(404, "File not found")
    return f


def delete_file(fid):
    f = get_file(fid)
    (proof_dir() / f["stored_name"]).unlink(missing_ok=True)
    db.ex("DELETE FROM payment_files WHERE id=?", (fid,))
    return f


def media_type(stored_name):
    return PROOF_TYPES.get(Path(stored_name).suffix.lower())


STATE_TEXT = {"paid": "已付款 Paid", "incomplete": "待补全 Incomplete", "overdue": "已逾期 Overdue", "pending": "未付款 Pending"}


def export_rows(cid):
    """Rows for the Excel export (payments sheet)."""
    rows = for_contract(cid)
    return [[p["seq"], p["trigger_en"], p["trigger_zh"], p["percentage"], p["amount"], p["currency"], p["due_rule"],
             p["due_date"], STATE_TEXT[p["state"]], p["paid_amount"], p["paid_date"],
             p["note"], p["recorded_by"], len(p["files"]), p["page"]] for p in rows]

