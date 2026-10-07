# -*- coding: utf-8 -*-
"""命令行：整频道/群复制到自建频道/群（不落地，服务端转发）。

例：
  python copy_chat_cli.py @srcchan --new-channel "备份" --dry-run
  python copy_chat_cli.py -1001234567890 --new-group "搬运群" --sender-block "@spammer"
  python copy_chat_cli.py @src --to @myarch --order size_desc --kw-block "广告,regex:^置顶"
  python copy_chat_cli.py @src --to @myarch --sender-allow "@好友,123456789" --anon exclude
"""
import argparse
import asyncio
import sys

import core
import mirror_core


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="频道/群一键复制（可过滤/去重/排序）")
    ap.add_argument("src", help="源群/频道：@用户名 / t.me 链接 / 数字ID")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--new-channel", metavar="标题", help="新建频道作为目标")
    g.add_argument("--new-group", metavar="标题", help="新建群组作为目标")
    g.add_argument("--to", metavar="目标", help="复制到已有频道/群")
    ap.add_argument("--about", default="", help="新建目标的简介")
    ap.add_argument("--order", default="time_asc", choices=mirror_core.ORDERS,
                    help="排序：time_asc=时间正序(默认原样复刻) time_desc=时间倒序 "
                         "size_asc/size_desc=按媒体大小")
    ap.add_argument("--sender-block", default="", help="排除这些成员（ID/@用户名/昵称，逗号分隔）")
    ap.add_argument("--sender-allow", default="", help="只保留这些成员的消息")
    ap.add_argument("--anon", choices=["keep", "exclude", "only"], default="keep",
                    help="无署名消息：keep=不管 exclude=排除 only=只保留")
    ap.add_argument("--kw-block", default="", help="含关键词排除（regex:前缀=正则）")
    ap.add_argument("--kw-allow", default="", help="只保留含关键词的消息")
    ap.add_argument("--media", choices=["all", "media_only", "text_only"], default="all")
    ap.add_argument("--exts", default="", help="只保留这些扩展名的媒体，如 pdf,mp4")
    ap.add_argument("--date-from", default="", help="只保留该日期（含）之后，2024-01-01")
    ap.add_argument("--date-to", default="", help="只保留该日期（含）之前")
    ap.add_argument("--limit", type=int, default=0, help="只扫最近 N 条，0=全部")
    ap.add_argument("--keep-author", action="store_true",
                    help="保留“转发自”头（默认去头，署名是你的账号）")
    ap.add_argument("--rebuild-replies", action="store_true",
                    help="逐条发送重建回复引用（慢，限流风险高，默认关闭）")
    ap.add_argument("--no-dedup-dest", action="store_true", help="不扫描目标已有同文件")
    ap.add_argument("--no-dedup-global", action="store_true", help="不使用全局去重库")
    ap.add_argument("--dry-run", action="store_true", help="只扫描统计，不创建目标不发送")
    a = ap.parse_args()

    def _split(s):
        return [x.strip() for x in (s or "").replace("，", ",").split(",") if x.strip()]

    rules = {
        "sender_block": _split(a.sender_block),
        "sender_allow": _split(a.sender_allow),
        "anonymous": a.anon,
        "keyword_block": _split(a.kw_block),
        "keyword_allow": _split(a.kw_allow),
        "media": a.media,
        "exts": _split(a.exts),
        "date_from": a.date_from,
        "date_to": a.date_to,
    }
    if a.new_channel:
        dest = ("new", "channel", a.new_channel, a.about)
    elif a.new_group:
        dest = ("new", "group", a.new_group, a.about)
    else:
        dest = ("existing", a.to)

    stats = asyncio.run(mirror_core.copy_chat(
        None, None, a.src, dest,
        {"rules": rules, "order": a.order, "drop_author": not a.keep_author,
         "rebuild_replies": a.rebuild_replies, "dedup_dest": not a.no_dedup_dest,
         "dedup_global": not a.no_dedup_global, "limit": a.limit,
         "dry_run": a.dry_run},
        log=lambda t: print(t, flush=True),
        on_progress=lambda d, tot, txt: print(f"[{d}/{tot}] {txt}", flush=True),
        cancelled=lambda: False))
    print("结果：", stats, flush=True)


if __name__ == "__main__":
    main()
