"""Background worker: read text (OCR) -> mask sensitive data -> LLM extraction -> waiting for review."""
import queue
import threading
import traceback

from . import db, payments
from .extract_text import read_any
from .llm import extract_contract
from .redact import redact_text

_q = queue.Queue()
_started = False


def enqueue(cid, stage="read"):
    db.set_status(cid, "queued", "排队中 / Queued")
    _q.put((cid, stage))


def _exists(cid):
    return bool(db.q("SELECT 1 FROM contracts WHERE id=?", (cid,), one=True))


def _process(cid, stage, current=None):
    current = current if current is not None else {}
    row = db.q("SELECT * FROM contracts WHERE id=?", (cid,), one=True)
    if not row:
        return
    if stage == "read" or not db.q("SELECT 1 FROM pages WHERE contract_id=?", (cid,)):
        current["stage"] = "read"
        db.set_status(cid, "reading", "读取文本 / Reading")
        path = db.FILES_DIR / row["stored_name"]
        pages = read_any(str(path), on_progress=lambda p: db.set_status(cid, "reading", p))
        masked_pages = [(page_no, text, *redact_text(text), is_ocr) for page_no, text, is_ocr in pages]
        with db._lock:  # check and write together, so a contract deleted meanwhile gets no orphan rows
            if not _exists(cid):
                return
            db.ex("DELETE FROM pages WHERE contract_id=?", (cid,))
            for page_no, text, masked, _n, is_ocr in masked_pages:
                db.ex("INSERT INTO pages (contract_id, page_no, text, redacted_text, is_ocr) VALUES (?,?,?,?,?)",
                      (cid, page_no, text, masked, 1 if is_ocr else 0))
            db.ex("UPDATE contracts SET page_count=?, ocr_pages=?, redaction_count=? WHERE id=?",
                  (len(pages), sum(1 for p in pages if p[2]), sum(p[3] for p in masked_pages), cid))
    current["stage"] = "extract"
    db.set_status(cid, "extracting", "AI 分析中 / AI extracting")
    pages = db.q("SELECT page_no, redacted_text FROM pages WHERE contract_id=? ORDER BY page_no", (cid,))
    data, model = extract_contract([(p["page_no"], p["redacted_text"]) for p in pages])
    with db._lock:
        if not _exists(cid):
            return
        db.save_data(cid, data, model)
        payments.sync_from_extraction(cid, data)  # payment nodes from the extracted payment terms
        db.set_status(cid, "review", None)
    from . import auth
    auth.log(None, "extracted", cid, {"model": model, "items": len(data.get("line_items") or [])})


def friendly_error(e, stage):
    """What went wrong, in words a user can act on. Technical details (paths, tracebacks) only go to the server log."""
    import json as _json
    import httpx
    from .extract_text import UnreadableFile
    msg = str(e)
    if isinstance(e, UnreadableFile):
        return msg
    if isinstance(e, FileNotFoundError):
        return "找不到这份合同的原件文件，请删除后重新上传 / The original file is missing; delete the contract and upload it again"
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return "连接 AI 服务超时，请稍后点“重新 AI 分析”重试；反复出现请管理员检查网络或在设置里调大超时 / " \
               "The AI service timed out. Try “Re-run AI” later; if it keeps happening ask an administrator"
    if isinstance(e, httpx.HTTPError):
        return "无法连接 AI 服务，请检查网络或让管理员在“设置”里测试连接 / Cannot reach the AI service; check the network or test it in Settings"
    if isinstance(e, (_json.JSONDecodeError, ValueError)) and stage == "extract":
        return "AI 返回的结果格式不对，请点“重新 AI 分析”重试 / The AI answer was not in the expected format; try “Re-run AI”"
    if isinstance(e, RuntimeError) and msg and stage == "extract":
        # messages from llm.py (missing API key, provider error, output truncated); never show file paths
        import re
        return re.sub(r"(?:[A-Za-z]:\\|/)[^\s'\"]+", "…", msg)[:500]
    if stage == "read":
        return "读取文件内容失败，请确认文件能正常打开，重新导出后再上传 / Could not read the file; check it opens normally and upload it again"
    return "AI 分析失败，请点“重新 AI 分析”重试；反复失败请联系管理员 / AI extraction failed; try “Re-run AI” or contact an administrator"


def _worker():
    while True:
        cid, stage = _q.get()
        current = {"stage": stage}
        try:
            _process(cid, stage, current)
        except Exception as e:
            traceback.print_exc()  # full technical detail stays in the server log
            try:  # recording the failure must never stop the worker: every later upload depends on it
                message = friendly_error(e, current["stage"])
                db.set_status(cid, "failed", None, message)
                from . import auth
                auth.log(None, "extract_failed", cid, {"error": message[:300], "type": type(e).__name__})
            except Exception:
                traceback.print_exc()
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
