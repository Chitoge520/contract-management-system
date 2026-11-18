"""AI contract assistant: private per-user conversations, read-only tools scoped to what the user may see,
streaming answers with citations, and usage quotas."""
import json
import re
import threading
import time
from datetime import datetime, timedelta

from fastapi import HTTPException

from . import access, auth, db, llm

MAX_TOOL_ROUNDS = 8
HISTORY_MESSAGES = 12
TOOL_RESULT_CHARS = 14000

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT,
    scope TEXT,                 -- JSON list of contract ids, NULL = all contracts the user may see
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at);
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
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS ai_usage (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    questions INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""


def init():
    with db._lock, db.connect() as c:
        c.executescript(SCHEMA)
        c.commit()


def today():
    return datetime.now().strftime("%Y-%m-%d")


# ======================= conversations =======================
def own_conversation(user, conv_id):
    conv = db.q("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"]), one=True)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    conv["scope"] = json.loads(conv["scope"]) if conv.get("scope") else None
    return conv


def scope_label(scope):
    if not scope:
        return None
    out = []
    for cid in scope:
        r = db.q("SELECT id, contract_no, title, title_zh FROM contracts WHERE id=?", (cid,), one=True)
        if r:
            out.append({"id": r["id"], "label": r["contract_no"] or r["title_zh"] or r["title"] or f"#{r['id']}"})
    return out


def allowed_ids(user, conv):
    ids = set(access.visible_ids(user))
    if conv.get("scope"):
        ids &= set(conv["scope"])
    return ids


# ======================= quotas =======================
def limits_for(user):
    cfg = llm.load_config()
    return {
        "questions": user.get("ai_daily_questions") if user.get("ai_daily_questions") is not None else int(cfg["ai_daily_questions"]),
        "tokens": user.get("ai_daily_tokens") if user.get("ai_daily_tokens") is not None else int(cfg["ai_daily_tokens"]),
        "global_tokens": int(cfg["ai_global_daily_tokens"]),
        "per_minute": int(cfg["ai_per_minute"]),
    }


def usage_for(user_id, day=None):
    r = db.q("SELECT questions, tokens FROM ai_usage WHERE user_id=? AND day=?", (user_id, day or today()), one=True)
    return r or {"questions": 0, "tokens": 0}


def check_quota(user):
    lim = limits_for(user)
    used = usage_for(user["id"])
    if used["questions"] >= lim["questions"]:
        raise HTTPException(429, f"今天的提问次数已用完（{lim['questions']} 次），明天再试或联系管理员 / Daily question limit reached")
    if used["tokens"] >= lim["tokens"]:
        raise HTTPException(429, "今天的 AI 用量已用完，明天再试或联系管理员 / Daily AI usage limit reached")
    total = db.q("SELECT coalesce(sum(tokens),0) n FROM ai_usage WHERE day=?", (today(),), one=True)["n"]
    if total >= lim["global_tokens"]:
        raise HTTPException(429, "系统今天的 AI 总用量已达上限，请联系管理员 / System-wide daily AI limit reached")
    since = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    recent = db.q("SELECT count(*) n FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                  "WHERE c.user_id=? AND m.role='user' AND m.created_at>=?", (user["id"], since), one=True)["n"]
    if recent >= lim["per_minute"]:
        raise HTTPException(429, "提问太频繁，请稍等一分钟 / Too many questions, wait a minute")


def add_usage(user_id, questions=0, tokens=0):
    with db._lock, db.connect() as c:
        c.execute("INSERT INTO ai_usage (user_id, day, questions, tokens) VALUES (?,?,?,?) "
                  "ON CONFLICT(user_id, day) DO UPDATE SET questions=questions+excluded.questions, tokens=tokens+excluded.tokens",
                  (user_id, today(), questions, tokens))
        c.commit()


# ======================= tools (read-only, scoped) =======================
TOOLS = [
    {"type": "function", "function": {
        "name": "search_contracts",
        "description": "Find contracts by keywords (matches number, title, parties, summary, scope, items and clauses) "
                       "and/or filters. Returns a list with id, number, title, supplier, type, total and status.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords in English or Chinese; leave empty to list all"},
            "contract_type": {"type": "string", "enum": ["software", "purchase", "lease", "service", "other"]},
            "party": {"type": "string", "description": "Part of the supplier / counter-party name"},
            "year": {"type": "string", "description": "Signing or effective year, e.g. 2026"},
            "limit": {"type": "integer", "description": "Max results, default 20"}}}}},
    {"type": "function", "function": {
        "name": "get_contract",
        "description": "Get the structured data of one contract: basic info, amounts, priced line items, payment terms, "
                       "milestones, warranty, penalties, key clauses, type-specific facts, summaries.",
        "parameters": {"type": "object", "required": ["contract_id"], "properties": {
            "contract_id": {"type": "integer"},
            "sections": {"type": "array", "items": {"type": "string", "enum": [
                "basic", "summary", "amount", "line_items", "payment_terms", "milestones", "warranty", "penalties",
                "key_clauses", "type_specific", "attachments", "risk_notes"]},
                "description": "Only these sections (default: all)"}}}}},
    {"type": "function", "function": {
        "name": "search_text",
        "description": "Full-text search in the original contract text (bank/tax numbers are masked). "
                       "Returns matching snippets with contract id and page number. Use it for details not in the structured data.",
        "parameters": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Words that must all appear on the page"},
            "contract_id": {"type": "integer", "description": "Limit to one contract"},
            "limit": {"type": "integer", "description": "Max snippets, default 8"}}}}},
    {"type": "function", "function": {
        "name": "read_pages",
        "description": "Read the full text of specific pages of a contract (max 4 pages per call).",
        "parameters": {"type": "object", "required": ["contract_id", "pages"], "properties": {
            "contract_id": {"type": "integer"},
            "pages": {"type": "array", "items": {"type": "integer"}}}}}},
    {"type": "function", "function": {
        "name": "list_line_items",
        "description": "List priced items (services, modules, goods) across contracts with their amounts.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words in the item name/description"},
            "contract_id": {"type": "integer"},
            "party": {"type": "string"},
            "limit": {"type": "integer", "description": "Max items, default 60"}}}}},
]


def _label(r):
    return r.get("contract_no") or r.get("title_zh") or r.get("title") or f"#{r['id']}"


def _ids_sql(ids):
    ids = sorted(ids)
    return ("(" + ",".join("?" * len(ids)) + ")", ids) if ids else ("(-1)", [])


def tool_search_contracts(ids, query="", contract_type=None, party=None, year=None, limit=20):
    marks, args = _ids_sql(ids)
    sql = f"SELECT * FROM contracts WHERE id IN {marks}"
    for w in (query or "").split():
        like = f"%{w.lower()}%"
        sql += (" AND (lower(coalesce(contract_no,'')||' '||coalesce(title,'')||' '||coalesce(title_zh,'')||' '||"
                "coalesce(party_a,'')||' '||coalesce(party_b,'')||' '||coalesce(data_json,'')) LIKE ?"
                " OR id IN (SELECT contract_id FROM pages WHERE lower(redacted_text) LIKE ?))")
        args += [like, like]
    if contract_type:
        sql += " AND contract_type=?"
        args.append(contract_type)
    if party:
        sql += " AND lower(coalesce(party_b,'')||' '||coalesce(party_a,'')) LIKE ?"
        args.append(f"%{party.lower()}%")
    if year:
        sql += " AND substr(coalesce(signing_date, effective_date, start_date, uploaded_at),1,4)=?"
        args.append(str(year))
    rows = db.q(sql + " ORDER BY id DESC LIMIT ?", args + [min(int(limit or 20), 50)])
    return [{"id": r["id"], "contract_no": r["contract_no"], "title": r["title"], "title_zh": r["title_zh"],
             "party_a": r["party_a"], "party_b": r["party_b"], "type": r["contract_type"],
             "total_amount": r["total_amount"], "currency": r["currency"], "signing_date": r["signing_date"],
             "review_status": "confirmed by a human reviewer" if r["status"] == "confirmed" else "AI-extracted, not yet reviewed",
             "contract_status": r["lifecycle_status"]} for r in rows]


SECTION_KEYS = {"basic": ["contract_type", "basic"], "summary": ["summary_zh", "summary_en", "scope_zh", "scope_en"],
                "amount": ["amount"], "line_items": ["line_items"], "payment_terms": ["payment_terms"],
                "milestones": ["milestones"], "warranty": ["warranty"], "penalties": ["penalties"],
                "key_clauses": ["key_clauses"], "type_specific": ["type_specific"], "attachments": ["attachments"],
                "risk_notes": ["risk_notes_zh"]}


def tool_get_contract(ids, contract_id, sections=None):
    contract_id = int(contract_id)
    if contract_id not in ids:
        return {"error": "Contract not found or not accessible"}
    r = db.q("SELECT * FROM contracts WHERE id=?", (contract_id,), one=True)
    data = json.loads(r["data_json"]) if r.get("data_json") else {}
    keys = [k for s in (sections or SECTION_KEYS) for k in SECTION_KEYS.get(s, [])]
    out = {"id": r["id"], "contract_no": r["contract_no"], "file_name": r["file_name"], "pages": r["page_count"],
           "review_status": "confirmed by a human reviewer" if r["status"] == "confirmed" else "AI-extracted, not yet reviewed",
           "contract_status": r["lifecycle_status"], "notes": r["notes"]}
    for k in keys:
        if k in data:
            out[k] = data[k]
    return out


def _snippet(text, word, width=220):
    i = text.lower().find(word.lower())
    if i < 0:
        return text[:width * 2]
    s, e = max(0, i - width), min(len(text), i + len(word) + width)
    return ("…" if s else "") + re.sub(r"\s+", " ", text[s:e]).strip() + ("…" if e < len(text) else "")


def tool_search_text(ids, query, contract_id=None, limit=8):
    words = [w.lower() for w in (query or "").split()]
    if not words:
        return []
    target = {int(contract_id)} & ids if contract_id else ids
    marks, args = _ids_sql(target)
    sql = f"SELECT p.contract_id, p.page_no, p.redacted_text, c.contract_no, c.title FROM pages p JOIN contracts c ON c.id=p.contract_id WHERE p.contract_id IN {marks}"
    for w in words:
        sql += " AND lower(p.redacted_text) LIKE ?"
        args.append(f"%{w}%")
    rows = db.q(sql + " ORDER BY p.contract_id DESC, p.page_no LIMIT ?", args + [min(int(limit or 8), 20)])
    return [{"contract_id": r["contract_id"], "contract_no": r["contract_no"], "page": r["page_no"],
             "snippet": _snippet(r["redacted_text"], words[0])} for r in rows]


def tool_read_pages(ids, contract_id, pages):
    contract_id = int(contract_id)
    if contract_id not in ids:
        return {"error": "Contract not found or not accessible"}
    pages = [int(p) for p in (pages or [])][:4]
    marks = ",".join("?" * len(pages)) or "-1"
    rows = db.q(f"SELECT page_no, redacted_text FROM pages WHERE contract_id=? AND page_no IN ({marks}) ORDER BY page_no",
                [contract_id] + pages)
    return [{"page": r["page_no"], "text": (r["redacted_text"] or "")[:5000]} for r in rows]


def tool_list_line_items(ids, query="", contract_id=None, party=None, limit=60):
    target = {int(contract_id)} & ids if contract_id else ids
    marks, args = _ids_sql(target)
    sql = (f"SELECT li.*, c.contract_no, c.party_b FROM line_items li JOIN contracts c ON c.id=li.contract_id "
           f"WHERE li.contract_id IN {marks}")
    for w in (query or "").split():
        sql += " AND lower(coalesce(li.name,'')||' '||coalesce(li.name_zh,'')||' '||coalesce(li.description,'')||' '||coalesce(li.category,'')) LIKE ?"
        args.append(f"%{w.lower()}%")
    if party:
        sql += " AND lower(coalesce(c.party_b,'')) LIKE ?"
        args.append(f"%{party.lower()}%")
    rows = db.q(sql + " ORDER BY c.id DESC, li.seq LIMIT ?", args + [min(int(limit or 60), 150)])
    return [{"contract_id": r["contract_id"], "contract_no": r["contract_no"], "party_b": r["party_b"], "item": r["name"],
             "item_zh": r["name_zh"], "description": r["description"], "quantity": r["quantity"], "unit_price": r["unit_price"],
             "amount": r["amount"], "currency": r["currency"], "page": r["page"]} for r in rows]


TOOL_FUNCS = {"search_contracts": tool_search_contracts, "get_contract": tool_get_contract, "search_text": tool_search_text,
              "read_pages": tool_read_pages, "list_line_items": tool_list_line_items}


def run_tool(ids, name, args):
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(ids, **{k: v for k, v in (args or {}).items() if v not in (None, "")})
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}


def step_label(name, args, result):
    """Short human-readable description of a tool call, shown under the answer."""
    a = args or {}
    n = len(result) if isinstance(result, list) else None
    if name == "search_contracts":
        return {"kind": "search", "text": a.get("query") or "", "count": n}
    if name == "get_contract":
        return {"kind": "contract", "contract_id": a.get("contract_id"), "sections": a.get("sections")}
    if name == "search_text":
        return {"kind": "text", "text": a.get("query"), "contract_id": a.get("contract_id"), "count": n}
    if name == "read_pages":
        return {"kind": "pages", "contract_id": a.get("contract_id"), "pages": a.get("pages")}
    if name == "list_line_items":
        return {"kind": "items", "text": a.get("query") or "", "count": n}
    return {"kind": name}


# ======================= the agent loop =======================
def system_prompt(user, conv):
    dept = access.department(user.get("department_id"))
    scope = scope_label(conv.get("scope"))
    scope_txt = ("ONLY these contracts: " + ", ".join(f"#{s['id']} {s['label']}" for s in scope)) if scope \
        else "all contracts this user is allowed to see"
    return f"""You are the contract assistant of SIPG Bayport Terminal (Party A) inside its contract register.
Today is {datetime.now():%Y-%m-%d}. You are talking to {user.get('display_name') or user['username']}"""\
f"""{', ' + user['position'] if user.get('position') else ''}{', ' + dept['name'] if dept else ''}.
Scope of this conversation: {scope_txt}.

How to work:
- Answer ONLY from data returned by your tools. Never guess or use outside knowledge about these contracts.
- Typical flow: search_contracts -> get_contract (only the sections you need) -> search_text / read_pages for wording details.
  Use list_line_items for questions about priced services/goods across contracts.
- If the information is not in the contracts, say so plainly.
- Cite every fact with the contract id and page in exactly this form: [#12 p.5] (or [#12] when no page is known).
  Use the numeric id from the tools, not the contract number.
- If a contract's review_status says it is not yet reviewed, mention that its data is AI-extracted and unverified.
- Masked values like [REDACTED] are intentionally hidden (bank / tax data). Never try to reveal or reconstruct them.
- You are read-only: you cannot change contracts. Refuse requests to modify data or to reveal other users' information.
- Reply in the language of the user's question (Chinese or English). Be concise; use Markdown tables for comparisons
  and lists of amounts. Always state currencies. Do not invent totals: only add up numbers the tools returned and show the parts."""


CITE_RE = re.compile(r"\[#(\d+)(?:\s*p\.?\s*(\d+))?\]")


def citations_for(text, ids):
    out, seen = [], set()
    for m in CITE_RE.finditer(text or ""):
        cid = int(m.group(1))
        if cid in ids and cid not in seen:
            seen.add(cid)
            r = db.q("SELECT id, contract_no, title, title_zh FROM contracts WHERE id=?", (cid,), one=True)
            if r:
                out.append({"id": cid, "label": _label(r), "title": r["title_zh"] or r["title"]})
    return out


def history_messages(conv_id, before_id):
    rows = db.q("SELECT role, content FROM messages WHERE conversation_id=? AND id<? AND content IS NOT NULL "
                "AND content<>'' ORDER BY id DESC LIMIT ?", (conv_id, before_id, HISTORY_MESSAGES))
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def answer(user, conv, question, cancel):
    """Generator of events for one question: start, step, delta, done / error. Persists everything."""
    ids = allowed_ids(user, conv)
    now = db.now()
    user_msg = db.ex("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                     (conv["id"], "user", question, now))
    if (conv.get("title") or "") in ("", "新对话", "New chat"):
        title = re.sub(r"\s+", " ", question).strip()[:40]
        db.ex("UPDATE conversations SET title=? WHERE id=?", (title, conv["id"]))
    db.ex("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv["id"]))
    add_usage(user["id"], questions=1)
    auth.log(user, "ai_question", detail={"conversation_id": conv["id"]})

    messages = [{"role": "system", "content": system_prompt(user, conv)}] + history_messages(conv["id"], user_msg) \
        + [{"role": "user", "content": question}]
    steps, text, tok_in, tok_out, status, error = [], "", 0, 0, "done", None
    cfg = llm.load_config()
    yield {"type": "start", "user_message_id": user_msg}
    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            use_tools = round_no < MAX_TOOL_ROUNDS
            if not use_tools:  # tool budget exhausted: ask for an answer with what we have
                messages.append({"role": "user", "content": "Answer now with the information gathered so far."})
            calls, round_text = None, ""
            for kind, val in llm.chat_stream(messages, TOOLS if use_tools else None, cfg, cancel=cancel):
                if kind == "content":
                    round_text += val
                    text += val
                    yield {"type": "delta", "text": val}
                elif kind == "tool_calls":
                    calls = val
                elif kind == "usage":
                    tok_in += int(val.get("prompt_tokens") or 0)
                    tok_out += int(val.get("completion_tokens") or 0)
            if cancel.is_set():
                status = "stopped"
                break
            if not calls:
                break
            messages.append({"role": "assistant", "content": round_text or None, "tool_calls": [
                {"id": c["id"] or f"call_{round_no}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for i, c in enumerate(calls)]})
            for i, c in enumerate(calls):
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = run_tool(ids, c["name"], args)
                step = step_label(c["name"], args, result)
                steps.append(step)
                yield {"type": "step", "step": step}
                body = json.dumps(result, ensure_ascii=False, default=str)
                if len(body) > TOOL_RESULT_CHARS:
                    body = body[:TOOL_RESULT_CHARS] + " …(truncated)"
                messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{round_no}_{i}", "content": body})
    except Exception as e:  # noqa: BLE001 - report any provider error to the user
        status, error = "error", str(e)[:500]
    if not tok_in and not tok_out:  # provider did not report usage: rough estimate
        tok_in = sum(len(m.get("content") or "") for m in messages) // 3
        tok_out = len(text) // 2
    cites = citations_for(text, ids)
    msg_id = db.ex("INSERT INTO messages (conversation_id, role, content, steps, citations, tokens_in, tokens_out, model, status, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (conv["id"], "assistant", text, json.dumps(steps, ensure_ascii=False), json.dumps(cites, ensure_ascii=False),
                    tok_in, tok_out, cfg.get("chat_model") or cfg.get("model"), status, db.now()))
    add_usage(user["id"], tokens=tok_in + tok_out)
    db.ex("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conv["id"]))
    if error:
        yield {"type": "error", "message": error, "message_id": msg_id}
    yield {"type": "done", "message_id": msg_id, "citations": cites, "status": status,
           "usage": {"tokens": tok_in + tok_out, **usage_for(user["id"])}}


def run_in_thread(user, conv, question):
    """Run the agent in a worker thread; returns (queue, cancel_event)."""
    import queue
    q, cancel = queue.Queue(), threading.Event()

    def work():
        try:
            for ev in answer(user, conv, question, cancel):
                q.put(ev)
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)[:500]})
        finally:
            q.put(None)
    threading.Thread(target=work, daemon=True).start()
    return q, cancel


def message_rows(conv_id):
    rows = db.q("SELECT id, role, content, steps, citations, status, created_at FROM messages WHERE conversation_id=? ORDER BY id",
                (conv_id,))
    for r in rows:
        r["steps"] = json.loads(r["steps"]) if r.get("steps") else []
        r["citations"] = json.loads(r["citations"]) if r.get("citations") else []
    return rows


def now_ts():
    return time.time()
