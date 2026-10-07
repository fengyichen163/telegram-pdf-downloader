# -*- coding: utf-8 -*-
"""本地备份的显示层：把消息计划渲染成聊天式 HTML（离线可看，观感对齐 Telegram）。

产出结构（staging 目录）：
  index.html            入口：统计 + 分页目录
  pages/page_001.html   每页一段消息（气泡聊天视图，前后翻页链接）
  media/…               媒体文件由备份编排下载到此，HTML 只做相对引用

要点：
- Telegram 实体偏移按 UTF-16 码元计算，直接用 Python 字符串切片会切错
  （emoji 占 2 个码元），entities_to_html 统一在 utf-16-le 字节层处理；
- 回复引用本地可完整还原（指针在本备份内解析，链到锚点）；
- 媒体永远是独立文件（不走 base64 内嵌），单页条数默认 500 控制体量。
"""
import html as html_mod
import os

from telethon import types

PAGE_SIZE = 500
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
_VIDEO_EXTS = {"mp4", "mkv", "mov", "webm"}
_AUDIO_EXTS = {"mp3", "ogg", "oga", "opus", "wav", "m4a"}

CSS = """
body{margin:0;background:#e6ebef;font-family:'Segoe UI','Microsoft YaHei',sans-serif}
.nav{background:#3390ec;color:#fff;padding:10px 16px;display:flex;gap:14px;align-items:baseline}
.nav b{font-size:15px}
.nav a{color:#dcebfd;text-decoration:none;font-size:13px}
.page{max-width:860px;margin:0 auto;padding:12px 16px 40px}
.daysep{text-align:center;margin:16px 0 8px;color:#7d8ea0;font-size:13px}
.msg{display:flex;margin:5px 0}
.msg.own{justify-content:flex-end}
.bubble{max-width:78%;background:#fff;border-radius:12px;padding:7px 11px;box-shadow:0 1px 1px rgba(0,0,0,.08)}
.own .bubble{background:#e2feca}
.sender{font-weight:600;font-size:13px;margin-bottom:2px}
.reply{border-left:3px solid #3390ec;background:#e8f2fc;padding:3px 8px;border-radius:4px;margin:2px 0 4px;font-size:13px;overflow:hidden}
.reply a{color:#3390ec;text-decoration:none}
.text{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.45}
.media img,.media video{max-width:340px;max-height:300px;border-radius:8px;display:block;margin-top:4px;cursor:pointer}
.media audio{width:320px;display:block;margin-top:4px}
.filecard{display:inline-block;margin-top:4px;padding:6px 10px;background:#f1f5f8;border-radius:8px;font-size:13px;text-decoration:none;color:#1c2733}
.meta{font-size:11px;color:#9aa7b1;text-align:right;margin-top:2px}
.anon{background:#98a5b1;color:#fff;font-size:10px;border-radius:3px;padding:1px 4px;margin-left:4px;vertical-align:middle}
.spoiler{background:#c4ccd2;color:transparent;border-radius:3px}
.spoiler:active{color:inherit;background:transparent}
pre{background:#f4f4f4;padding:6px;border-radius:6px;overflow:auto;font-size:13px}
code{background:#f4f4f4;padding:1px 4px;border-radius:3px;font-size:13px}
blockquote{border-left:3px solid #c9d3da;margin:4px 0;padding:2px 8px;color:#5f6b76}
a{color:#3390ec}
.statstable{border-collapse:collapse;font-size:13px;margin:10px 0}
.statstable td{border:1px solid #d5dde3;padding:5px 12px}
.tocline{padding:6px 10px;background:#fff;border-radius:8px;margin:6px 0;font-size:13px}
"""


def entities_to_html(text, entities):
    """Telethon 实体 → HTML。偏移按 UTF-16 码元，嵌套实体只保留最外层（简化）。"""
    if not text:
        return ""
    b = text.encode("utf-16-le")
    spans = sorted(((e.offset * 2, (e.offset + e.length) * 2, e)
                    for e in entities or []), key=lambda s: (s[0], -s[1]))
    out = []
    pos = 0

    def esc(a, z):
        return html_mod.escape(b[a:z].decode("utf-16-le", "replace"))

    for start, end, ent in spans:
        if start < pos or end > len(b):
            continue
        out.append(esc(pos, start))
        inner = html_mod.escape(b[start:end].decode("utf-16-le", "replace"))
        out.append(_wrap(ent, inner))
        pos = end
    out.append(esc(pos, len(b)))
    return "".join(out)


def _wrap(ent, inner):
    if isinstance(ent, types.MessageEntityBold):
        return f"<b>{inner}</b>"
    if isinstance(ent, types.MessageEntityItalic):
        return f"<i>{inner}</i>"
    if isinstance(ent, types.MessageEntityUnderline):
        return f"<u>{inner}</u>"
    if isinstance(ent, types.MessageEntityStrike):
        return f"<s>{inner}</s>"
    if isinstance(ent, types.MessageEntityCode):
        return f"<code>{inner}</code>"
    if isinstance(ent, types.MessageEntityPre):
        lang = html_mod.escape(getattr(ent, "language", "") or "")
        return f'<pre><code>{inner}</code></pre>' if not lang else \
            f'<pre><code data-lang="{lang}">{inner}</code></pre>'
    if isinstance(ent, types.MessageEntityTextUrl):
        url = html_mod.escape(ent.url, quote=True)
        return f'<a href="{url}" target="_blank">{inner}</a>'
    if isinstance(ent, types.MessageEntitySpoiler):
        return f'<span class="spoiler">{inner}</span>'
    if isinstance(ent, types.MessageEntityBlockquote):
        return f"<blockquote>{inner}</blockquote>"
    return inner


def _media_html(entry):
    """按扩展名渲染媒体标签；文件缺失（未下载/失败）时给占位卡片。"""
    name = entry.get("mfile")
    if not name:
        return ""
    rel = f"media/{html_mod.escape(name, quote=True)}"
    ext = (entry.get("ext") or "").lower()
    size = core_hsize(entry.get("size") or 0)
    if ext in _IMAGE_EXTS:
        return (f'<div class="media"><a href="{rel}" target="_blank">'
                f'<img loading="lazy" src="{rel}" alt=""></a></div>')
    if ext in _VIDEO_EXTS:
        return f'<div class="media"><video controls preload="none" src="{rel}"></video></div>'
    if ext in _AUDIO_EXTS:
        return f'<div class="media"><audio controls preload="none" src="{rel}"></audio></div>'
    return (f'<div class="media"><a class="filecard" href="{rel}" target="_blank">📎 '
            f'{html_mod.escape(name)}（{size}）</a></div>')


def core_hsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _bubble(entry, summaries):
    """单条消息 → 气泡 HTML。summaries: id→(页码, 摘要文本, 署名) 供引用还原。"""
    sid = entry["id"]
    cls = "msg own" if entry.get("own") else "msg"
    name = html_mod.escape(entry.get("sender_name") or "")
    anon = ('<span class="anon">匿名</span>' if entry.get("anonymous")
            else (f'<span class="anon">频道</span>'
                  if entry.get("sender_id") is None else ""))
    body = []
    rep = entry.get("reply_id")
    if rep and rep in summaries:
        page, sname, stext = summaries[rep]
        stext = html_mod.escape(stext[:80])
        sname = html_mod.escape(sname or "")
        body.append(f'<div class="reply"><a href="pages/page_{page:03d}.html#m{rep}">'
                    f'↩ {sname}: {stext or "（媒体）"}</a></div>')
    body.append(_media_html(entry))
    txt = entities_to_html(entry.get("text") or "", entry.get("entities") or None)
    if txt:
        body.append(f'<div class="text">{txt}</div>')
    if not body:
        return ""
    sender_html = f'<div class="sender">{name}{anon}</div>' if (name or anon) else ""
    meta = f'<div class="meta">{entry.get("time") or ""}</div>'
    return (f'<div class="{cls}" id="m{sid}"><div class="bubble">'
            f'{sender_html}{"".join(body)}{meta}</div></div>')


def render(staging, title, entries, stats, page_size=PAGE_SIZE):
    """渲染 index + 分页；返回页文件相对路径列表（按顺序）。"""
    pages_dir = os.path.join(staging, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    # 摘要表：id → (页码, 署名, 摘要)，供回复引用本地跳转
    summaries = {}
    for i, e in enumerate(entries):
        summaries[e["id"]] = (i // page_size + 1,
                              e.get("sender_name") or "",
                              (e.get("text") or "").replace("\n", " "))

    page_ids = []
    n_pages = (len(entries) + page_size - 1) // page_size or 1
    for pi in range(n_pages):
        chunk = entries[pi * page_size:(pi + 1) * page_size]
        parts = [_page_head(title, pi + 1, n_pages)]
        prev_date = None
        for e in chunk:
            if e["date"] and e["date"] != prev_date:
                parts.append(f'<div class="daysep">{e["date"]}</div>')
                prev_date = e["date"]
            parts.append(_bubble(e, summaries))
        parts.append(_page_tail(title, pi + 1, n_pages))
        path = os.path.join(pages_dir, f"page_{pi + 1:03d}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        page_ids.append(pi + 1)

    media_cnt = sum(1 for e in entries if e.get("mfile"))
    media_bytes = sum(e.get("size") or 0 for e in entries if e.get("mfile"))
    toc = "".join(
        f'<div class="tocline"><a href="pages/page_{p:03d}.html">第 {p} / {n_pages} 页'
        f'（{entries[(p - 1) * page_size]["date"]} 起）</a></div>'
        for p in page_ids)
    with open(os.path.join(staging, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{html_mod.escape(title)} — 备份</title>"
                f"<style>{CSS}</style></head><body>"
                f"<div class='nav'><b>{html_mod.escape(title)}</b>"
                f"<a href='index.html'>目录</a></div><div class='page'>"
                f"<table class='statstable'>"
                f"<tr><td>消息数</td><td>{len(entries)}</td>"
                f"<td>媒体</td><td>{media_cnt} 个 / {core_hsize(media_bytes)}</td></tr>"
                f"<tr><td>扫描总数</td><td>{stats.get('scanned', '-')}</td>"
                f"<td>过滤排除</td><td>{stats.get('filtered_out', '-')}</td></tr>"
                f"<tr><td>导出时间</td><td colspan='3'>{stats.get('exported_at', '-')}</td></tr>"
                f"</table>{toc}</div></body></html>")
    return [f"pages/page_{p:03d}.html" for p in page_ids]


def _page_head(title, pi, n_pages):
    prev = f'<a href="page_{pi - 1:03d}.html">← 上一页</a>' if pi > 1 else "<span>　</span>"
    nxt = f'<a href="page_{pi + 1:03d}.html">下一页 →</a>' if pi < n_pages else "<span>　</span>"
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html_mod.escape(title)} · {pi}/{n_pages}</title>"
            f"<style>{CSS}</style></head><body>"
            f"<div class='nav'><b>{html_mod.escape(title)}</b> · 第 {pi}/{n_pages} 页"
            f"<a href='../index.html'>目录</a>{prev}{nxt}</div><div class='page'>")


def _page_tail(title, pi, n_pages):
    prev = f'<a href="page_{pi - 1:03d}.html">← 上一页</a>' if pi > 1 else "<span>　</span>"
    nxt = f'<a href="page_{pi + 1:03d}.html">下一页 →</a>' if pi < n_pages else "<span>　</span>"
    return (f"</div><div class='nav'>{prev}<a href='../index.html'>目录</a>{nxt}"
            f"</div></body></html>")


def page_media_bytes(entries, page_size=PAGE_SIZE):
    """每页的媒体字节量（分卷打包用）。"""
    out = []
    for pi in range((len(entries) + page_size - 1) // page_size or 1):
        chunk = entries[pi * page_size:(pi + 1) * page_size]
        out.append(sum(e.get("size") or 0 for e in chunk))
    return out
