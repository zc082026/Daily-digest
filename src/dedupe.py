"""去重：给每条内容算一个稳定的指纹，用来判断"这条是不是已经推过"。

三级策略：
1. URL 规范化后哈希 —— 最可靠，能去掉 utm 等追踪参数造成的假重复
2. 标题指纹 —— 有些站点同一篇文章会换 URL 重发
3. SimHash —— 标题轻微改写（加前缀、改标点）也能识别为同一条
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 这些参数只用于追踪来源，不影响内容，去重时应忽略
_TRACKING_PREFIXES = ("utm_", "spm", "from", "ref", "share", "_ga", "wt_")
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), netloc, path, urlencode(sorted(query)), "")
    )


def title_key(title: str) -> str:
    """标题归一化：去掉所有标点与空白，转小写。"""
    return _PUNCT.sub("", (title or "").lower())


def simhash(text: str, bits: int = 64) -> int:
    """轻量 SimHash：把文本切成 2-gram 再加权累加，用于识别近似标题。"""
    tokens = [text[i : i + 2] for i in range(len(text) - 1)] or [text]
    vec = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vec[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vec[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def fingerprint(title: str, url: str) -> str:
    norm_url = normalize_url(url)
    if norm_url:
        return "u:" + hashlib.sha1(norm_url.encode("utf-8")).hexdigest()[:16]
    return "t:" + hashlib.sha1(title_key(title).encode("utf-8")).hexdigest()[:16]
