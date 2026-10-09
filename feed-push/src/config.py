"""配置加载：读取 config/sources.yaml，并把 ${ENV_VAR} 占位符替换为环境变量。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
STATE_PATH = ROOT / "data" / "state.json"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Source:
    id: str
    name: str
    type: str
    url: str
    enabled: bool = True
    keywords: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)


@dataclass
class Settings:
    max_items_per_run: int = 40
    max_summary_per_run: int = 20
    max_items_per_source: int = 10
    time_window_hours: int = 72


def _expand_env(value: Any) -> Any:
    """把字符串里的 ${VAR} 换成环境变量，便于把带密钥的地址写进配置。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_config() -> tuple[Settings, list[Source]]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)

    sec = raw.get("settings") or {}
    settings = Settings(
        max_items_per_run=int(sec.get("max_items_per_run", 40)),
        max_summary_per_run=int(sec.get("max_summary_per_run", 20)),
        max_items_per_source=int(sec.get("max_items_per_source", 10)),
        time_window_hours=int(sec.get("time_window_hours", 72)),
    )

    known = {"id", "name", "type", "url", "enabled", "keywords"}
    sources: list[Source] = []
    for idx, item in enumerate(raw.get("sources") or []):
        sid = str(item.get("id") or f"source-{idx}")
        sources.append(
            Source(
                id=sid,
                name=str(item.get("name") or sid),
                type=str(item.get("type") or "rss").lower(),
                url=str(item.get("url") or ""),
                enabled=bool(item.get("enabled", True)),
                keywords=[str(k) for k in (item.get("keywords") or [])],
                extra={k: v for k, v in item.items() if k not in known},
            )
        )
    return settings, sources
