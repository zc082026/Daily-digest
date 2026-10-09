"""主流程：采集 -> 去重 -> 摘要 -> 推送 -> 落盘。

单次运行即退出，由 GitHub Actions 定时唤起。
"""

from __future__ import annotations

import sys
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import notifier
from .config import Source, load_config
from .dedupe import fingerprint
from .fetchers import Item, fetch_source, within_window
from .store import Store, now_cn
from .summarizer import enabled as llm_enabled
from .summarizer import item_key, summarize_many

WORKERS = 5


def safe_fetch(src: Source, max_items: int) -> tuple[list[Item], str | None]:
    try:
        return fetch_source(src, max_items), None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def collect(sources: list[Source], max_items: int) -> tuple[dict[str, list[Item]], dict[str, str]]:
    items_by_source: dict[str, list[Item]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(safe_fetch, s, max_items): s for s in sources}
        for future, src in futures.items():
            items, err = future.result()
            if err:
                errors[src.id] = err
                print(f"[采集] 失败 {src.name} ({src.id}): {err}")
            else:
                items_by_source[src.id] = items
                print(f"[采集] 成功 {src.name}: {len(items)} 条")
    return items_by_source, errors


def build_report(candidates, summaries, sources_total, sources_ok, now):
    period = "早报" if now.hour < 12 else "晚报"
    header = (
        f"# 信息{period} · {now.strftime('%m月%d日 %H:%M')}\n\n"
        f"> 新增 <font color=\"info\">{len(candidates)}</font> 条 · "
        f"数据源 {sources_ok}/{sources_total} 正常\n"
    )

    grouped: dict[str, list[tuple[str, Item]]] = {}
    for fp, item in candidates:
        grouped.setdefault(item.source_name, []).append((fp, item))

    lines: list[str] = []
    for name, group in grouped.items():
        lines.append(f"## {name}")
        for _fp, item in group:
            lines.append(f"- [{item.title}]({item.url})")
            summary = summaries.get(item_key(item))
            if summary:
                lines.append(f"  > {summary}")
        lines.append("")
    return header, lines


def build_alert(unhealthy, name_map):
    lines = ["## 数据源异常", "以下数据源连续多次抓取失败，可能已改版或失效：", ""]
    for sid, rec in unhealthy:
        name = name_map.get(sid, sid)
        lines.append(f"- **{name}**：连续失败 {rec.get('fail_streak')} 次")
        if rec.get("last_error"):
            lines.append(f"  > {rec['last_error'][:150]}")
    lines.append("")
    lines.append("如已废弃，请在 config/sources.yaml 中把 enabled 改为 false。")
    return "\n".join(lines)


def main() -> int:
    settings, sources = load_config()
    active = [s for s in sources if s.enabled and s.url]
    print(f"[启动] 启用源 {len(active)}/{len(sources)} 个")

    if not active:
        print("[启动] 没有启用任何数据源，退出。请在 config/sources.yaml 中添加。")
        return 0

    store = Store()
    items_by_source, errors = collect(active, settings.max_items_per_source)

    for src in active:
        if src.id in errors:
            store.record_failure(src.id, errors[src.id])
        else:
            store.record_success(src.id)

    candidates: list[tuple[str, Item]] = []
    seen: set[str] = set()
    for src in active:
        for item in items_by_source.get(src.id, []):
            if not within_window(item, settings.time_window_hours):
                continue
            fp = fingerprint(item.title, item.url)
            if fp in seen or store.is_sent(fp):
                continue
            seen.add(fp)
            candidates.append((fp, item))

    truncated = False
    if len(candidates) > settings.max_items_per_run:
        candidates = candidates[: settings.max_items_per_run]
        truncated = True

    print(f"[去重] 待推送 {len(candidates)} 条" + ("（已截断）" if truncated else ""))

    summaries: dict[str, str] = {}
    if candidates and llm_enabled():
        summaries = summarize_many(
            [it for _fp, it in candidates], settings.max_summary_per_run
        )
        print(f"[摘要] 成功 {len(summaries)} 条")
    elif candidates:
        print("[摘要] 未配置 LLM_API_KEY，仅推送标题")

    sources_ok = len(active) - len(errors)
    unhealthy = store.unhealthy(threshold=3)

    pushed = False
    if candidates:
        now = now_cn()
        header, lines = build_report(
            candidates, summaries, len(active), sources_ok, now
        )
        try:
            count = notifier.send_chunks(lines, header)
            print(f"[推送] 已发送 {count} 条消息")
            pushed = True
        except Exception as exc:  # noqa: BLE001
            print(f"[推送] 失败：{exc}")
            traceback.print_exc()
    else:
        print("[推送] 无新增内容，本次静默")

    if unhealthy:
        name_map = {s.id: s.name for s in sources}
        try:
            notifier.send_markdown(build_alert(unhealthy, name_map))
            print(f"[告警] 已推送 {len(unhealthy)} 个异常源")
        except Exception as exc:  # noqa: BLE001
            print(f"[告警] 推送失败：{exc}")

    if pushed:
        for fp, item in candidates:
            store.mark_sent(fp, item.title, item.source_name)
    store.prune()
    store.save()
    print("[完成] 状态已保存")
    return 0


if __name__ == "__main__":
    sys.exit(main())
