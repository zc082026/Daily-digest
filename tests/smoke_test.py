"""自检脚本：改完配置后先跑一次，确认采集、去重、推送格式都正常。

用法（在项目根目录执行）：
    pip install -r requirements.txt
    python tests/smoke_test.py

不需要配置任何密钥，也不会真的往群里发消息。
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Source, load_config  # noqa: E402
from src.dedupe import fingerprint, hamming, normalize_url, simhash, title_key  # noqa: E402
from src.fetchers import Item, fetch_source, within_window  # noqa: E402
from src.main import build_report  # noqa: E402
from src.notifier import split_markdown  # noqa: E402
from src.store import CN_TZ, Store  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {extra}")


print("\n[1] 配置加载")
settings, sources = load_config()
check("配置文件可解析", isinstance(sources, list))
print(f"        max_items_per_run={settings.max_items_per_run}")

print("\n[2] URL 规范化与指纹")
u = normalize_url("https://WWW.Example.com/a/b/?utm_source=x&id=1&spm=zz#frag")
check("去掉 www / 追踪参数 / fragment", u == "https://example.com/a/b?id=1", u)
check("不同追踪参数指纹一致",
      fingerprint("T", "https://a.com/p?utm_source=1")
      == fingerprint("T", "https://a.com/p?utm_source=2"))
check("标题对标点与空白不敏感",
      title_key("关于 2026 年预算的通知！") == title_key("关于2026年预算的通知"))

print("\n[3] SimHash 近似识别")
h1 = simhash(title_key("关于2026年度预算编制的通知"))
h2 = simhash(title_key("【转发】关于2026年度预算编制的通知"))
h3 = simhash(title_key("今天食堂菜单更新"))
check("近似标题距离小", hamming(h1, h2) < 20, f"d={hamming(h1, h2)}")
check("无关标题距离大", hamming(h1, h3) > 10, f"d={hamming(h1, h3)}")

print("\n[4] 状态存储")
with tempfile.TemporaryDirectory() as td:
    st = Store(Path(td) / "state.json")
    check("初始为空", not st.is_sent("x"))
    st.mark_sent("x", "标题", "源A")
    check("标记后可查", st.is_sent("x"))
    st.save()
    check("落盘后可恢复", Store(Path(td) / "state.json").is_sent("x"))
    st.record_failure("s1", "boom")
    st.record_failure("s1", "boom")
    check("连续 2 次不告警", len(st.unhealthy(3)) == 0)
    st.record_failure("s1", "boom")
    check("连续 3 次触发告警", len(st.unhealthy(3)) == 1)
    st.record_success("s1")
    check("成功后计数清零", len(st.unhealthy(3)) == 0)

print("\n[5] 消息分片（企业微信单条上限约 4096 字节）")
lines = [f"- [第{i}条标题](https://example.com/{i})" + "x" * 120 for i in range(60)]
chunks = split_markdown(lines, "# 头部")
check("分片数量大于 1", len(chunks) > 1, f"n={len(chunks)}")
check("每片不超 3800 字节", all(len(c.encode()) <= 3800 for c in chunks))
check("内容无丢失", sum(c.count("- [") for c in chunks) == 60)

print("\n[6] 报告组装")
src = Source(id="s", name="测试源", type="rss", url="")
items = [
    Item(title=f"标题{i}", url=f"https://e.com/{i}", source_id="s", source_name="测试源")
    for i in range(3)
]
cands = [(fingerprint(it.title, it.url), it) for it in items]
header, body = build_report(cands, {}, 10, 10, datetime.now())
joined = "\n".join(body)
check("头部含数量", "新增" in header)
check("含来源分组", "## 测试源" in joined)
check("含链接", "https://e.com/0" in joined)

print("\n[7] 时间窗过滤")
_now = datetime.now(CN_TZ)
check("24 小时内放行",
      within_window(Item("新", "https://e.com/1", "s", "源",
                         published=_now - timedelta(hours=24)), 72))
check("超过 72 小时过滤",
      not within_window(Item("旧", "https://e.com/2", "s", "源",
                             published=_now - timedelta(hours=100)), 72))
check("无时间戳放行", within_window(Item("无日期", "https://e.com/3", "s", "源"), 72))

print("\n[8] 真实 RSS 连通性（需要网络，失败不影响其他项）")
try:
    got = fetch_source(
        Source(id="live", name="连通性测试", type="rss",
               url="https://www.ruanyifeng.com/blog/atom.xml"),
        5,
    )
    check("抓取到条目", len(got) > 0, f"n={len(got)}")
    for it in got[:3]:
        print(f"        - {it.title[:40]}")
except Exception as exc:  # noqa: BLE001
    print(f"        跳过（网络不可用）：{type(exc).__name__}: {exc}")

print("\n[9] Webhook 地址校验（防止填错成机器人资料页链接）")
import os  # noqa: E402

import src.notifier as N  # noqa: E402

os.environ.pop("WECOM_WEBHOOK", None)
os.environ.pop("WECHAT_WEBHOOK", None)
try:
    N.webhook_url()
    check("未配置时报错", False)
except RuntimeError as exc:
    check("未配置时报错", "未配置" in str(exc))

os.environ["WECOM_WEBHOOK"] = (
    "https://work.weixin.qq.com/wework_admin/common/openBotProfile/abc123"
)
try:
    N.webhook_url()
    check("拒绝资料页链接", False)
except RuntimeError as exc:
    check("拒绝资料页链接", "格式不对" in str(exc))

os.environ["WECOM_WEBHOOK"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"
check("接受正确地址", N.webhook_url().endswith("key=test"))
os.environ.pop("WECOM_WEBHOOK", None)

print(f"\n结果：通过 {ok}，失败 {fail}")
sys.exit(1 if fail else 0)
