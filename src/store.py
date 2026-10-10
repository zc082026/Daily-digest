"""状态存储：一个 JSON 文件，跟踪"已推送"和"源健康状况"。

GitHub Actions 每次运行都是全新机器，本地文件会丢失，
所以状态必须落盘并随仓库一起提交，下次运行时读回来。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import STATE_PATH

CN_TZ = timezone(timedelta(hours=8))
KEEP_DAYS = 30


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE_PATH
        self.data: dict[str, Any] = {"sent": {}, "sources": {}, "last_run": None}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.setdefault("sent", {})
                    self.data.setdefault("sources", {})
                    self.data.update(loaded)
                    self.data.setdefault("sent", {})
                    self.data.setdefault("sources", {})
            except json.JSONDecodeError:
                print(f"[store] 状态文件损坏，已重置：{self.path}")

    def save(self) -> None:
        self.data["last_run"] = now_cn().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 已推送记录 ----------
    def is_sent(self, fingerprint: str) -> bool:
        return fingerprint in self.data["sent"]

    def mark_sent(self, fingerprint: str, title: str, source: str) -> None:
        self.data["sent"][fingerprint] = {
            "at": now_cn().isoformat(timespec="seconds"),
            "title": title[:120],
            "source": source,
        }

    def prune(self, days: int = KEEP_DAYS) -> int:
        """只保留最近 N 天记录，避免仓库无限膨胀。"""
        cutoff = now_cn() - timedelta(days=days)
        before = len(self.data["sent"])
        self.data["sent"] = {
            fp: meta
            for fp, meta in self.data["sent"].items()
            if _parse(meta.get("at")) is None or _parse(meta["at"]) >= cutoff
        }
        return before - len(self.data["sent"])

    # ---------- 源健康 ----------
    def record_success(self, source_id: str) -> None:
        rec = self.data["sources"].setdefault(source_id, {})
        rec.update(
            last_success=now_cn().isoformat(timespec="seconds"),
            fail_streak=0,
            last_error="",
        )

    def record_failure(self, source_id: str, error: str) -> None:
        rec = self.data["sources"].setdefault(source_id, {})
        rec["fail_streak"] = int(rec.get("fail_streak", 0)) + 1
        rec["last_error"] = str(error)[:200]
        rec["last_failure"] = now_cn().isoformat(timespec="seconds")

    def unhealthy(self, threshold: int = 3) -> list[tuple[str, dict]]:
        out = []
        for sid, rec in self.data["sources"].items():
            if int(rec.get("fail_streak", 0)) >= threshold:
                out.append((sid, rec))
        return out


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
