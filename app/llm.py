"""LLM extraction through any OpenAI-compatible Chat Completions API (DeepSeek, Qwen/DashScope, etc.)."""
import json
import os
import re
from pathlib import Path

import httpx

from .db import DATA_DIR

CONFIG_PATH = DATA_DIR / "config.json"

PROVIDERS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "qwen": {"label": "通义千问 Qwen (中国站)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model": "qwen-plus"},
    "qwen_intl": {"label": "Qwen (International)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "custom": {"label": "自定义 OpenAI 兼容 / Custom", "base_url": "", "model": ""},
}

DEFAULT_CONFIG = {"provider": "deepseek", "base_url": PROVIDERS["deepseek"]["base_url"],
                  "model": PROVIDERS["deepseek"]["model"], "api_key": "", "max_tokens": 8192, "timeout": 300,
                  # AI assistant
                  "chat_model": "",                  # empty = same as extraction model
                  "ai_daily_questions": 100,         # per user per day (users can have an override)
                  "ai_daily_tokens": 500000,         # per user per day
                  "ai_global_daily_tokens": 5000000, # whole system per day
                  "ai_per_minute": 6}                # per user per minute

MAX_INPUT_CHARS = 180_000


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(new):
    cfg = load_config()
    for k in DEFAULT_CONFIG:
        if k in new and new[k] is not None:
            if k == "api_key" and new[k] == "__keep__":
                continue
            cfg[k] = new[k]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


SYSTEM_PROMPT = """You are a meticulous contract analyst working for Party A's finance & audit team.
You read a contract (text extracted page by page; page boundaries are marked "===== PAGE n =====")
and return ONE JSON object that follows the schema below exactly. Rules:
- Use ONLY information stated in the contract. Never guess. Unknown -> null (or [] for lists).
- Amounts: plain numbers without thousand separators (e.g. 66000). Percentages as numbers (30 for 30%).
- Dates: "YYYY-MM-DD" only if a concrete date is written; otherwise null and describe the rule in the *_rule / text fields.
- "page": the page number (from the PAGE markers) where the information appears.
- line_items: list EVERY priced item / service / module / goods line from the price table(s), in order, including
  each sub-module with its own price. If a row has no own price (merged/part of a group), still list it with amount null.
  Keep "name" and "description" in the contract's original language; add a concise Chinese "name_zh".
- Text marked [REDACTED] was masked on purpose (bank / tax data). Ignore it; never try to reconstruct it.
- Summaries: summary_zh in Simplified Chinese, summary_en in English, each 3-6 sentences, covering what is bought/provided,
  the price, the time frame and the most important obligations.
- contract_type: one of "software" (software/IT development or integration), "purchase" (goods/hardware/software licence
  purchase), "lease" (rental of assets/premises/equipment), "service" (services, maintenance, consulting, outsourcing), "other".
- type_specific: key facts specific to the contract type, e.g. lease: leased asset, lease term, rent & frequency, deposit,
  renewal/return terms; purchase: delivery place/terms (Incoterms), delivery date, quantities, acceptance, warranty;
  service: service scope, service period, service levels, billing basis; software: delivery period, deliverables, SLA levels.

JSON schema:
{
  "contract_type": "software|purchase|lease|service|other",
  "basic": {
    "contract_no": str|null, "title": str|null, "title_zh": str|null,
    "party_a": str|null, "party_a_address": str|null, "party_b": str|null, "party_b_address": str|null,
    "signing_date": str|null, "effective_date": str|null, "effective_date_rule": str|null,
    "start_date": str|null, "end_date": str|null, "term_text": str|null,
    "governing_law": str|null, "dispute_resolution": str|null, "language": str|null
  },
  "summary_zh": str, "summary_en": str,
  "scope_zh": str, "scope_en": str,
  "amount": {
    "currency": str|null, "settlement_currency": str|null, "total_amount": number|null,
    "total_amount_text": str|null, "tax_note": str|null, "exchange_rate_rule": str|null, "page": int|null
  },
  "line_items": [
    {"category": str|null, "name": str, "name_zh": str|null, "description": str|null,
     "quantity": number|null, "unit": str|null, "unit_price": number|null, "amount": number|null,
     "currency": str|null, "page": int|null}
  ],
  "payment_terms": [
    {"seq": int, "trigger": str, "trigger_zh": str, "percentage": number|null, "amount": number|null,
     "due_rule": str|null, "conditions": str|null, "page": int|null}
  ],
  "milestones": [
    {"name": str, "name_zh": str|null, "deadline_rule": str|null, "deadline_date": str|null,
     "deliverables": str|null, "page": int|null}
  ],
  "warranty": {"period": str|null, "start_rule": str|null, "content": str|null, "page": int|null},
  "penalties": [
    {"type": str, "type_zh": str|null, "rule": str, "cap": str|null, "page": int|null}
  ],
  "key_clauses": [
    {"topic": str, "topic_zh": str, "section": str|null, "summary_en": str, "summary_zh": str, "page": int|null}
  ],
  "type_specific": [
    {"label": str, "label_zh": str, "value": str, "page": int|null}
  ],
  "attachments": [ {"name": str, "summary_zh": str|null, "page": int|null} ],
  "risk_notes_zh": [str]
}
key_clauses should cover (when present): term & termination, liability & limitation, confidentiality, IP,
force majeure, acceptance, SLA / maintenance response, governing law & disputes.
risk_notes_zh: short notes on anything Party A's finance/audit should pay attention to (e.g. blank signing date,
amounts in table not matching the total, unusual payment conditions)."""


def build_user_prompt(pages):
    parts = []
    for page_no, text in pages:
        parts.append(f"===== PAGE {page_no} =====\n{text.strip()}")
    body = "\n\n".join(parts)
    truncated = False
    if len(body) > MAX_INPUT_CHARS:
        body = body[:MAX_INPUT_CHARS]
        truncated = True
    return ("Extract the contract below into the JSON schema. Return JSON only.\n\n" + body), truncated


def _parse_json(content):
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def chat(messages, cfg=None, json_mode=True, max_tokens=None):
    cfg = cfg or load_config()
    fake = os.environ.get("CMS_FAKE_LLM")
    if fake:  # test hook: return a canned response file
        return Path(fake).read_text(encoding="utf-8")
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 API Key，请到“设置”页面填写 / API key not configured (Settings)")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0,
               "max_tokens": max_tokens or int(cfg.get("max_tokens") or 8192)}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(timeout=float(cfg.get("timeout") or 300), trust_env=True) as client:
        r = client.post(url, json=payload, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"LLM API error {r.status_code}: {r.text[:500]}")
    data = r.json()
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("模型输出被截断（合同条目过多），请在设置中调大 max_tokens / output truncated, raise max_tokens")
    return choice["message"]["content"]


def extract_contract(pages):
    cfg = load_config()
    prompt, truncated = build_user_prompt(pages)
    content = chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}], cfg)
    data = _parse_json(content)
    if truncated:
        data.setdefault("risk_notes_zh", []).append("合同文本过长，仅分析了前部分内容，请人工核对后续页。")
    return data, cfg.get("model")


def test_connection(cfg):
    content = chat([{"role": "user", "content": 'Reply with JSON {"ok": true}'}], cfg, json_mode=True, max_tokens=20)
    return content


def chat_stream(messages, tools=None, cfg=None, max_tokens=4096, cancel=None):
    """Stream a chat completion. Yields ("content", text), ("tool_calls", [...]) once at the end if the model
    asked for tools, and ("usage", {"prompt_tokens", "completion_tokens"}) when the API reports it.
    `cancel` is a threading.Event; setting it stops reading (and closes the HTTP stream)."""
    cfg = cfg or load_config()
    if os.environ.get("CMS_FAKE_AGENT"):
        from .fake_agent import fake_stream
        yield from fake_stream(messages, tools, cancel)
        return
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 API Key，请联系管理员在“设置”中填写 / API key not configured")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg.get("chat_model") or cfg["model"], "messages": messages, "temperature": 0.2,
               "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    calls = {}
    with httpx.Client(timeout=httpx.Timeout(float(cfg.get("timeout") or 300), connect=20), trust_env=True) as client:
        with client.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                body = r.read().decode("utf-8", "ignore")
                raise RuntimeError(f"LLM API error {r.status_code}: {body[:400]}")
            for line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    return
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    yield "usage", chunk["usage"]
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        yield "content", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
    if calls:
        yield "tool_calls", [calls[k] for k in sorted(calls)]
