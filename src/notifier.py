"""企业微信群机器人推送。

要点：
- Webhook 地址等同于密码，只能放在 Secrets 里
- 单条 markdown 上限约 4096 字节，超长必须分片
- 频率限制 20 条/分钟，分片之间需要间隔
"""

from __future__ import annotations

import json
import os
import time

import httpx

MAX_BYTES = 3800  # 留出余量，官方上限约 4096
RETRIES = 3

# 把企业微信的错误码翻译成人话，避免对着一串数字干瞪眼
ERR_HINTS = {
    93000: "Webhook 无效或被安全设置拦截。创建机器人时不要勾选「加签」和「IP 段白名单」，"
           "GitHub Actions 的出口 IP 是随机变动的，无法加入白名单。",
    45009: "推送太频繁（限制 20 条/分钟），程序会自动重试。",
    40058: "消息参数不合法，通常是内容超长或格式有误。",
    41001: "Webhook 地址不完整，请确认复制了完整地址（含 ?key= 后面那串）。",
}


def _explain(code: int, errmsg: str) -> str:
    hint = ERR_HINTS.get(code)
    base = f"errcode={code} {errmsg}"
    return f"{base}｜{hint}" if hint else base


PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def webhook_url() -> str:
    url = (os.environ.get("WECOM_WEBHOOK") or os.environ.get("WECHAT_WEBHOOK") or "").strip()
    if not url:
        raise RuntimeError("未配置 WECOM_WEBHOOK，无法推送")

    if not url.startswith(PREFIX):
        raise RuntimeError(
            "Webhook 地址格式不对。正确的地址必须以\n"
            f"  {PREFIX}?key=xxxxxx\n开头。"
            "注意：管理后台里形如 work.weixin.qq.com/.../openBotProfile/... 的"
            "「机器人资料页」链接不是 Webhook，请在群设置的「群机器人」里复制真正的地址。"
        )
    if "key=" not in url:
        raise RuntimeError("Webhook 地址缺少 ?key= 部分，请重新完整复制一次")
    return url


def _post(payload: dict) -> None:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(webhook_url(), json=payload)
                data = resp.json()
            if data.get("errcode") == 0:
                return
            last = RuntimeError(_explain(data.get("errcode"), data.get("errmsg")))
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"推送失败：{last}")


def send_markdown(content: str) -> None:
    _post({"msgtype": "markdown", "markdown": {"content": content}})


def send_text(content: str) -> None:
    _post({"msgtype": "text", "text": {"content": content[:2000]}})


def split_markdown(lines: list[str], header: str = "") -> list[str]:
    """按字节长度把若干行切成多条消息，保证每条不超限。"""
    chunks: list[str] = []
    current: list[str] = [] if not header else [header]

    def size_of(parts: list[str]) -> int:
        return len("\n".join(parts).encode("utf-8"))

    for line in lines:
        if size_of(current + [line]) > MAX_BYTES and len(current) > (1 if header else 0):
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_chunks(lines: list[str], header: str = "") -> int:
    """分片发送，遵守 20 条/分钟限制。返回发送条数。"""
    chunks = split_markdown(lines, header)
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(3.5)
        send_markdown(chunk)
    return len(chunks)


def _debug_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)[:200]
