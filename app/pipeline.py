"""Background worker: read text (OCR) -> mask sensitive data -> LLM extraction -> waiting for review."""
import queue
import threading
import traceback

from . import db
from .extract_text import read_any
from .llm import extract_contract
from .redact import redact_text

_q = queue.Queue()
_started = False


def enqueue(cid, stage="read"):
    db.set_status(cid, "queued", "排队中 / Queued")
    _q.put((cid, stage))


def _process(cid, stage):
    row = db.q("SELECT * FROM contracts WHERE id=?", (cid,), one=True)
    if not row:
        return
    if stage == "read" or not db.q("SELECT 1 FROM pages WHERE contract_id=?", (cid,)):
        db.set_status(cid, "reading", "读取文本 / Reading")
        path = db.FILES_DIR / row["stored_name"]
        pages = read_any(str(path), on_progress=lambda p: db.set_status(cid, "reading", p))
        total_masked = 0
        db.ex("DELETE FROM pages WHERE contract_id=?", (cid,))
        for page_no, text, is_ocr in pages:
            masked, n = redact_text(text)
            total_masked += n
            db.ex("INSERT INTO pages (contract_id, page_no, text, redacted_text, is_ocr) VALUES (?,?,?,?,?)",
                  (cid, page_no, text, masked, 1 if is_ocr else 0))
        db.ex("UPDATE contracts SET page_count=?, ocr_pages=?, redaction_count=? WHERE id=?",
              (len(pages), sum(1 for p in pages if p[2]), total_masked, cid))
    db.set_status(cid, "extracting", "AI 分析中 / AI extracting")
    pages = db.q("SELECT page_no, redacted_text FROM pages WHERE contract_id=? ORDER BY page_no", (cid,))
    data, model = extract_contract([(p["page_no"], p["redacted_text"]) for p in pages])
    db.save_data(cid, data, model)
    db.set_status(cid, "review", None)


def _worker():
    while True:
        cid, stage = _q.get()
        try:
            _process(cid, stage)
        except Exception as e:  # keep worker alive
            traceback.print_exc()
            db.set_status(cid, "failed", None, str(e)[:1000])
        finally:
            _q.task_done()


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True).start()
    # resume unfinished jobs after a restart
    for r in db.q("SELECT id, status FROM contracts WHERE status IN ('queued','reading','extracting')"):
        enqueue(r["id"], "read" if r["status"] in ("queued", "reading") else "extract")
