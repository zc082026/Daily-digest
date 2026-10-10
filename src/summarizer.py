"""AI 摘要：调用任意 OpenAI 兼容接口，给每条内容生成一句话结论。

设计原则：摘要是增强，不是必需。任何失败（没配 Key、超时、额度用完）
都只降级为"标题 + 链接"，绝不阻塞推送。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from .fetchers import Item

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

PROMPT = """请用一句不超过 40 字的中文，概括下面这条信息的核心结论或事实。
只输出这一句话，不要前缀、不要解释、不要引号。

标题：{title}
{body}"""


def enabled() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def summarize_one(item: Item) -> str | None:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None

    base_url = os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL

    body = item.raw_summary.strip()
    body_line = f"正文摘要：{body[:500]}" if body else ""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": PROMPT.format(title=item.title, body=body_line),
            }
        ],
        "temperature": 0.2,
        "max_tokens": 120,
    }
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(
                _endpoint(base_url),
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        text = (
            data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        )
        return text.replace("\n", " ").strip() or None
    except Exception as exc:  # noqa: BLE001
        print(f"[summary] 失败（降级为仅标题）：{type(exc).__name__} {exc}")
        return None


def summarize_many(items: list[Item], limit: int, workers: int = 4) -> dict[str, str]:
    """只对前 limit 条做摘要，返回 {指纹: 摘要}。"""
    targets = items[:limit]
    if not targets:
        return {}

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(summarize_one, it): it for it in targets}
        for future, item in futures.items():
            text = future.result()
            if text:
                results[item_key(item)] = text
    return results


def item_key(item: Item) -> str:
    return f"{item.source_id}::{item.url}::{item.title}"
