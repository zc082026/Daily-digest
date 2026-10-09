"""采集器：按源类型取回条目。单个源失败会被捕获，不影响其他源。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import Source
from .store import CN_TZ

# 注意：HTTP 头必须是纯 ASCII，这里不能写中文，否则 httpx 编码会直接报错
UA = "Mozilla/5.0 (compatible; DailyDigestBot/1.0; +https://github.com/)"
TIMEOUT = 20.0
RETRIES = 2


@dataclass
class Item:
    title: str
    url: str
    source_id: str
    source_name: str
    published: datetime | None = None
    raw_summary: str = ""


def _get(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            with httpx.Client(
                timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{last}")


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000
        return datetime.fromtimestamp(ts, CN_TZ)
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=CN_TZ)
    if isinstance(value, str):
        text = value.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
            "%a, %d %b %Y %H:%M:%S %z",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _dig(data: Any, path: str) -> Any:
    """按 a.b.c 逐层取值，path 为 '.' 或空时返回原对象。"""
    if not path or path == ".":
        return data
    cur = data
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _absolute(url: str, prefix: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if prefix:
        return prefix.rstrip("/") + "/" + url.lstrip("/")
    return url


def fetch_rss(src: Source) -> list[Item]:
    content = _get(src.url)
    feed = feedparser.parse(content)
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise RuntimeError(f"RSS 解析失败：{getattr(feed, 'bozo_exception', '未知')}")

    items: list[Item] = []
    for entry in feed.entries[: src.get("max_items", 10)]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        items.append(
            Item(
                title=title,
                url=link,
                source_id=src.id,
                source_name=src.name,
                published=_parse_time(entry.get("published_parsed"))
                or _parse_time(entry.get("updated_parsed")),
                raw_summary=(entry.get("summary") or "")[:300],
            )
        )
    return items


def fetch_json(src: Source) -> list[Item]:
    import json as _json

    content = _get(src.url)
    data = _json.loads(content.decode("utf-8", errors="replace"))
    raw_items = _dig(data, src.get("items_path", "."))
    if raw_items is None:
        raise RuntimeError(f"JSON 中找不到列表：items_path={src.get('items_path')}")
    if isinstance(raw_items, dict):
        raw_items = [raw_items]

    prefix = src.get("url_prefix", "") or ""
    t_key = src.get("title_key", "title")
    u_key = src.get("url_key", "url")
    d_key = src.get("date_key", "")

    items: list[Item] = []
    for row in list(raw_items)[: src.get("max_items", 10)]:
        if not isinstance(row, dict):
            continue
        title = str(row.get(t_key) or "").strip()
        link = _absolute(str(row.get(u_key) or "").strip(), prefix)
        if not title:
            continue
        items.append(
            Item(
                title=title,
                url=link,
                source_id=src.id,
                source_name=src.name,
                published=_parse_time(row.get(d_key)) if d_key else None,
            )
        )
    return items


def fetch_html(src: Source) -> list[Item]:
    selector = src.get("item_selector")
    if not selector:
        raise RuntimeError("html 类型必须配置 item_selector")

    content = _get(src.url)
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    nodes = soup.select(selector)
    if not nodes:
        raise RuntimeError(f"页面上找不到元素：{selector}")

    t_sel = src.get("title_selector", "a")
    l_sel = src.get("link_selector", "a")
    attr = src.get("link_attr", "href")
    prefix = src.get("url_prefix", "") or ""

    items: list[Item] = []
    for node in nodes[: src.get("max_items", 10)]:
        t_node = node.select_one(t_sel) if t_sel else node
        if t_node is None:
            continue
        title = t_node.get_text(strip=True)
        if not title:
            continue
        l_node = node.select_one(l_sel) if l_sel else node
        link = ""
        if l_node is not None:
            link = l_node.get(attr, "") if attr != "text" else l_node.get_text(strip=True)
        items.append(
            Item(
                title=title,
                url=_absolute(str(link).strip(), prefix),
                source_id=src.id,
                source_name=src.name,
            )
        )
    return items


FETCHERS = {"rss": fetch_rss, "json": fetch_json, "html": fetch_html}


def fetch_source(src: Source, max_items: int) -> list[Item]:
    src.extra.setdefault("max_items", max_items)
    fn = FETCHERS.get(src.type)
    if fn is None:
        raise RuntimeError(f"不支持的源类型：{src.type}")
    return fn(src)


def within_window(item: Item, hours: int) -> bool:
    """没有时间戳的条目一律放行（交给去重判断新旧）。"""
    if item.published is None:
        return True
    now = datetime.now(CN_TZ)
    if item.published > now + timedelta(hours=1):
        return True  # 时间戳明显是未来（时区问题），也放行
    return item.published >= now - timedelta(hours=hours)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
