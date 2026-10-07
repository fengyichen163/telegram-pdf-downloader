# -*- coding: utf-8 -*-
"""命令行：频道/群完整备份到本地（含纯文本），支持过滤/排序/两种打包形态。

例：
  python backup_cli.py @srcchan -o D:\\bak --dry-run            # 只扫描统计
  python backup_cli.py @srcchan -o D:\\bak                      # 散件式（逐个文件+聊天索引）
  python backup_cli.py @srcchan -o D:\\bak --mode zip           # 打包单压缩包
  python backup_cli.py @srcchan -o D:\\bak --mode volumes       # 打包分卷（每卷 3.8GB）
  python backup_cli.py @src -o D:\\bak --sender-block "@x" --order size_desc --kw-block "广告"
"""
import argparse
import asyncio
import sys

import core
import mirror_core


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="频道/群本地备份（过滤/排序/预览/打包）")
    ap.add_argument("src", help="源群/频道：@用户名 / t.me 链接 / 数字ID")
    ap.add_argument("-o", "--out", required=True, help="保存目录")
    ap.add_argument("--mode", choices=["files", "zip", "volumes"], default="files",
                    help="files=散件式 zip=单压缩包 volumes=分卷压缩包（默认 files）")
    ap.add_argument("--vol-gb", type=float, default=3.8, help="分卷单卷上限 GB（默认 3.8）")
    ap.add_argument("--order", default="time_asc", choices=mirror_core.ORDERS)
    ap.add_argument("--sender-block", default="")
    ap.add_argument("--sender-allow", default="")
    ap.add_argument("--anon", choices=["keep", "exclude", "only"], default="keep")
    ap.add_argument("--kw-block", default="")
    ap.add_argument("--kw-allow", default="")
    ap.add_argument("--media", choices=["all", "media_only", "text_only"], default="all")
    ap.add_argument("--exts", default="", help="只保留这些扩展名的媒体，如 jpg,mp4")
    ap.add_argument("--date-from", default="")
    ap.add_argument("--date-to", default="")
    ap.add_argument("--limit", type=int, default=0, help="只扫最近 N 条，0=全部")
    ap.add_argument("--workers", type=int, default=3, help="媒体下载并行数 1~8")
    ap.add_argument("--dry-run", action="store_true", help="只扫描统计，不下载不导出")
    a = ap.parse_args()

    def _split(s):
        return [x.strip() for x in (s or "").replace("，", ",").split(",") if x.strip()]

    rules = {"sender_block": _split(a.sender_block), "sender_allow": _split(a.sender_allow),
             "anonymous": a.anon, "keyword_block": _split(a.kw_block),
             "keyword_allow": _split(a.kw_allow), "media": a.media,
             "exts": _split(a.exts),
             "date_from": a.date_from, "date_to": a.date_to}

    async def run():
        async with core.connected_client(core.SESSION_PATH, None, None) as client:
            if not await client.is_user_authorized():
                raise SystemExit("尚未登录，请先登录。")
            import backup_core
            if a.dry_run:
                entries, stats, _src, title = await backup_core.plan_backup(
                    client, a.src, rules, a.order, a.limit,
                    log=lambda t: print(t, flush=True), cancelled=lambda: False)
                print(f"预览：{title}，{len(entries)} 条，媒体 {stats['media_cnt']} 个 / "
                      f"{core.human_size(stats['media_bytes'])}（过滤排除 {stats['filtered_out']}）。"
                      "未下载未导出。", flush=True)
                return None
            return await backup_core.export_backup(
                None, None, a.src, a.out, a.mode,
                {"rules": rules, "order": a.order, "limit": a.limit,
                 "workers": a.workers, "vol_gb": a.vol_gb},
                log=lambda t: print(t, flush=True),
                on_progress=lambda pct, tot, txt: print(f"[{pct:.0f}%] {txt}", flush=True),
                cancelled=lambda: False)

    stats = asyncio.run(run())
    if stats:
        print("结果：", {k: v for k, v in stats.items() if k != "zip_files"},
              flush=True)
        for z in stats.get("zip_files") or []:
            print("包：", z, flush=True)


if __name__ == "__main__":
    main()
