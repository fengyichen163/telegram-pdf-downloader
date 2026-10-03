# -*- coding: utf-8 -*-
"""Telegram 群/频道 PDF 批量下载器（命令行版）。

用法:
    python pdf_downloader_cli.py @channel            # 下载 @channel 全部 PDF
    python pdf_downloader_cli.py t.me/xxx -o D:\\pdfs
    python pdf_downloader_cli.py -1001234567890 -n 500

首次运行会引导登录（输入验证码 / 两步验证密码），会话保存在本目录，
api_id / api_hash 也会写进 config.json 供下次使用。
"""
import argparse
import asyncio
import getpass
import json
import os
import sys
import traceback

import core


def load_cfg():
    try:
        with open(core.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    old = load_cfg()
    old.update(cfg)
    try:
        with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


async def run(args, api_id, api_hash, phone):
    client = core.make_client(core.SESSION_PATH, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        if not phone:
            await client.disconnect()
            raise RuntimeError(
                "尚未登录且没有保存的手机号。请先双击 download_pdfs.bat "
                "在图形界面登录，或用 --phone 参数提供手机号。"
            )
        await core.login_flow(
            client, phone, log=print,
            ask_code=lambda: input("验证码: "),
            ask_password=lambda: getpass.getpass("两步验证密码: "),
        )
    me = await client.get_me()
    print(f"已登录：{core.display_name(me)}")
    await client.disconnect()

    return await core.run_download(
        api_id, api_hash, args.chat, args.out, args.limit,
        log=print,
        on_scan=lambda n: print(f"\r已扫描 {n} 条消息…", end="", flush=True),
        on_found=lambda n, tb: print(
            f"\n找到 {n} 个 PDF（{core.human_size(tb)}），{args.workers} 条连接并行下载…"),
        on_progress=lambda df, tf, bd, tb, name: print(
            f"\r[{df}/{tf}] {core.human_size(bd)}/{core.human_size(tb)} {name[:40]}   ",
            end="", flush=True),
        cancelled=lambda: False,
        workers=args.workers,
    )


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Telegram 群/频道 PDF 批量下载器")
    ap.add_argument("chat", nargs="?", help="群/频道：@用户名、t.me/链接 或 数字 ID")
    ap.add_argument("-o", "--out", default=core.DEFAULT_OUT, help="输出目录（默认：下载文件夹）")
    ap.add_argument("-n", "--limit", type=int, default=0, help="最多扫描的消息数，0=全部")
    ap.add_argument("-w", "--workers", type=int, default=4, help="并行下载连接数（1-8，默认 4）")
    ap.add_argument("--api-id", type=int, help="my.telegram.org 的 api_id（也可写入 config.json）")
    ap.add_argument("--api-hash", help="my.telegram.org 的 api_hash")
    ap.add_argument("--phone", help="手机号（国际格式，如 +8613800138000）")
    args = ap.parse_args()
    if not args.chat:
        ap.error("请给出群/频道，例如：python pdf_downloader_cli.py @durov")

    cfg = load_cfg()
    api_id = args.api_id or cfg.get("api_id")
    api_hash = args.api_hash or cfg.get("api_hash")
    phone = args.phone or cfg.get("phone")
    if not (api_id and api_hash):
        print("首次使用请到 https://my.telegram.org 申请 api_id / api_hash（详见 README）。")
        api_id = api_id or input("api_id: ").strip()
        api_hash = api_hash or input("api_hash: ").strip()
    api_id = int(api_id)
    save_cfg({"api_id": api_id, "api_hash": api_hash, "phone": phone,
              "out_dir": args.out, "chat": args.chat, "limit": args.limit,
              "workers": args.workers})

    try:
        ok, skip, fail = asyncio.run(run(args, api_id, api_hash, phone))
        print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}。文件在：{args.out}")
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
    except Exception as e:
        core.log_file(traceback.format_exc())
        print(f"\n出错：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
