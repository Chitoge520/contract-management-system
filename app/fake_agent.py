"""Scripted stand-in for the LLM, used only by automated tests (env CMS_FAKE_AGENT=1).
It behaves like a tool-calling model: search -> open the first contract -> answer with citations."""
import json
import time


def _turn_tools(messages):
    out = []
    for m in reversed(messages):
        if m["role"] == "user":
            break
        if m["role"] == "tool":
            out.insert(0, json.loads(m["content"].split(" …(truncated)")[0]))
    return out


def fake_stream(messages, tools, cancel=None):
    done = _turn_tools(messages)
    question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if tools and not done:
        yield "usage", {"prompt_tokens": 900, "completion_tokens": 20}
        yield "tool_calls", [{"id": "c1", "name": "search_contracts", "arguments": json.dumps({"query": "", "limit": 5})}]
        return
    if tools and len(done) == 1 and isinstance(done[0], list) and done[0]:
        yield "usage", {"prompt_tokens": 1100, "completion_tokens": 25}
        yield "tool_calls", [{"id": "c2", "name": "get_contract",
                              "arguments": json.dumps({"contract_id": done[0][0]["id"], "sections": ["basic", "amount", "line_items"]})}]
        return
    data = done[-1] if done and isinstance(done[-1], dict) else {}
    if not data or data.get("error"):
        text = "合同中没有找到相关内容。"
    else:
        cid = data["id"]
        rows = "\n".join(f"| {it.get('name_zh') or it.get('name')} | {it.get('currency') or ''} {it.get('amount'):,.0f} | [#{cid} p.{it.get('page')}] |"
                         for it in (data.get("line_items") or [])[:4] if it.get("amount") is not None)
        amt = data.get("amount") or {}
        text = (f"关于“{question[:20]}”：合同 **{data.get('contract_no')}** 总金额为 "
                f"{amt.get('currency')} {amt.get('total_amount'):,.0f} [#{cid} p.{amt.get('page')}]。主要明细如下：\n\n"
                f"| 项目 | 金额 | 出处 |\n|---|---|---|\n{rows}\n\n> 注意：该合同数据为 AI 提取，尚未人工复核。")
    for i in range(0, len(text), 6):
        if cancel is not None and cancel.is_set():
            return
        yield "content", text[i:i + 6]
        time.sleep(0.03)
    yield "usage", {"prompt_tokens": 2400, "completion_tokens": len(text) // 2}
