# -*- coding: utf-8 -*-
"""消息过滤引擎（频道复制 / 本地备份共用）。纯函数，规则 dict 进、bool 出。

规则 dict（全部可选）：
{
  "sender_block":  ["123456789", "@user", "显示名"],  # 排除这些成员的消息
  "sender_allow":  [],                                # 非空 = 只保留这些成员的消息
  "anonymous":     "keep",      # keep=不管 | exclude=排除无署名 | only=只保留有署名的
  "keyword_block": ["广告", "regex:^置顶"],           # 含关键词排除（regex: 前缀=正则）
  "keyword_allow": [],                                # 非空 = 只保留含关键词的消息
  "media":         "all",       # all | media_only | text_only
  "exts":          ["pdf"],     # 非空 = 只保留这些扩展名的媒体
  "date_from":     "",          # "2024-01-01"（含），按消息日期
  "date_to":       "",          # "2024-12-31"（含）
}

"无署名"的定义（见 mirror_core._sender_meta）：
  - 群里以群身份发言（匿名管理员）
  - 发送者实体暂时拿不到的消息
  频道帖不算（署名即频道本身）。
"""
import re


def _norm(x):
    return (x or "").strip().lstrip("@").lower()


def _kw_compile(items):
    out = []
    for it in items or []:
        it = (it or "").strip()
        if not it:
            continue
        if it.lower().startswith("regex:"):
            try:
                out.append(("re", re.compile(it[6:], re.IGNORECASE)))
            except re.error:
                continue
        else:
            out.append(("sub", it.lower()))
    return out


def compile_rules(rules):
    """预编译正则/集合，避免每条消息重复编译；非法正则静默忽略。"""
    rules = rules or {}
    return {
        "sender_block": {_norm(x) for x in rules.get("sender_block") or [] if _norm(x)},
        "sender_allow": {_norm(x) for x in rules.get("sender_allow") or [] if _norm(x)},
        "anonymous": rules.get("anonymous") or "keep",
        "kw_block": _kw_compile(rules.get("keyword_block")),
        "kw_allow": _kw_compile(rules.get("keyword_allow")),
        "media": rules.get("media") or "all",
        "exts": {(e or "").strip().lstrip(".").lower()
                 for e in rules.get("exts") or [] if (e or "").strip()},
        "date_from": (rules.get("date_from") or "").strip(),
        "date_to": (rules.get("date_to") or "").strip(),
    }


def _kw_hit(kws, text):
    for kind, kw in kws:
        if kind == "re":
            if kw.search(text):
                return True
        elif kw in text:
            return True
    return False


def matches(meta, r):
    """meta 键：sender_id, sender_name, sender_username, anonymous,
    text, has_media, ext, date（"YYYY-MM-DD"）。"""
    sid = meta.get("sender_id")
    sid_s = str(sid) if sid is not None else ""
    uname = _norm(meta.get("sender_username"))
    name = (meta.get("sender_name") or "").strip().lower()

    def in_set(s):
        return (bool(sid_s) and sid_s in s) or (bool(uname) and uname in s) \
            or (bool(name) and name in s)

    if r["sender_allow"] and not in_set(r["sender_allow"]):
        return False
    if r["sender_block"] and in_set(r["sender_block"]):
        return False
    anon = bool(meta.get("anonymous"))
    if r["anonymous"] == "exclude" and anon:
        return False
    if r["anonymous"] == "only" and not anon:
        return False
    text = (meta.get("text") or "").lower()
    if r["kw_allow"] and not _kw_hit(r["kw_allow"], text):
        return False
    if r["kw_block"] and _kw_hit(r["kw_block"], text):
        return False
    has_media = bool(meta.get("has_media"))
    if r["media"] == "media_only" and not has_media:
        return False
    if r["media"] == "text_only" and has_media:
        return False
    if r["exts"]:
        if not has_media or (meta.get("ext") or "").lower() not in r["exts"]:
            return False
    d = meta.get("date") or ""
    if r["date_from"] and d < r["date_from"]:
        return False
    if r["date_to"] and d > r["date_to"]:
        return False
    return True
