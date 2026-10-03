# -*- coding: utf-8 -*-
"""把 AyuGram / Telegram Desktop 的 tdata 登录凭证转换成下载器会话。

用法:
    python convert_tdata.py               # 转换 AyuGram 当前活跃的账号
    python convert_tdata.py 1             # 转换指定账号索引（0 = 主账号）
    python convert_tdata.py --passcode X  # tdata 设了本地密码时使用

只读取 tdata，不改动它，不影响 AyuGram 的登录。转换后免验证码直接可用。
原理：解密 tdata 的本地密钥 → 取出当前账号的 MTProto 授权密钥 →
写入 Telethon 会话文件（session_downloader.session）。
"""
import argparse
import asyncio
import json
import os
import sys

from PyQt5.QtCore import QByteArray, QDataStream
from telethon import TelegramClient
from telethon.crypto.authkey import AuthKey as TelethonAuthKey
from telethon.sessions import SQLiteSession

from opentele.api import API
from opentele.td import auth as au
from opentele.td import storage as st

import core

# Telegram 生产环境 DC 地址（IPv4, 443）
DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
K_WIDE_IDS_TAG = ~0 & 0xFFFFFFFFFFFFFFFF  # Account.kWideIdsTag


def read_accounts(tdata, passcode=b""):
    """解析 tdata，返回 [(账号索引, userId, mainDcId, auth_key_bytes)]。"""
    kd = st.Storage.ReadFile("key_data", tdata)  # ReadFile 自动补 s → key_datas
    salt, key_enc, info_enc = QByteArray(), QByteArray(), QByteArray()
    kd.stream >> salt >> key_enc >> info_enc

    passcode_key = st.Storage.CreateLocalKey(salt, QByteArray(passcode))
    inner = st.Storage.DecryptLocal(key_enc, passcode_key)
    local_key = au.AuthKey(inner.stream.readRawData(256))

    info = st.Storage.DecryptLocal(info_enc, local_key)
    s = info.stream
    count = s.readInt32()
    indexes = [s.readInt32() for _ in range(range_ok(count))]
    active = 0
    if not s.atEnd():
        active = s.readInt32()

    accounts = []
    for idx in indexes:
        base = "data" if idx == 0 else f"data#{idx + 1}"
        folder = st.Storage.ToFilePart(st.Storage.ComputeDataNameKey(base))
        try:
            f = st.Storage.ReadEncryptedFile(folder, tdata, local_key)
        except BaseException:
            continue  # 未登录的空壳账号目录
        if f.stream.readInt32() != 75:  # 75 = MTP authorization 块
            continue
        blob = QByteArray()
        f.stream >> blob
        ms = QDataStream(blob)
        ms.setVersion(QDataStream.Version.Qt_5_1)

        user_id = ms.readInt32()
        dc_id = ms.readInt32()
        if (user_id << 32 | dc_id) & 0xFFFFFFFFFFFFFFFF == K_WIDE_IDS_TAG:
            user_id = ms.readUInt64()
            dc_id = ms.readInt32()

        def read_key_list():
            n = ms.readInt32()
            out = []
            for _ in range(max(0, n)):
                kdc = ms.readInt32()
                data = bytes(ms.readRawData(256))
                if len(data) == 256:
                    out.append((kdc, data))
            return out

        keys = read_key_list()
        read_key_list()  # mtpKeysToDestroy，忽略

        auth_key = next((k for kdc, k in keys if kdc == dc_id),
                        keys[0][1] if keys else None)
        if auth_key:
            accounts.append({"index": idx, "user_id": user_id, "dc_id": dc_id,
                             "auth_key": auth_key, "active": idx == active})
    return accounts, active


def range_ok(n):
    return max(0, min(n, 8))


async def build_session(api, account, proxy=None):
    """把授权密钥写进 SQLiteSession 并联网验证。"""
    sess = SQLiteSession(core.SESSION_PATH)
    ip = DC_IPS.get(account["dc_id"], DC_IPS[2])
    sess.set_dc(account["dc_id"], ip, 443)
    sess.auth_key = TelethonAuthKey(account["auth_key"])
    sess.save()

    kwargs = {}
    if proxy:
        kwargs["proxy"] = {"proxy_type": proxy["type"], "addr": proxy["host"],
                           "port": int(proxy["port"])}
    client = TelegramClient(sess, api.api_id, api.api_hash, **kwargs)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return None
        return await client.get_me()
    finally:
        await client.disconnect()


def save_config(api, proxy=None):
    cfg = {}
    try:
        with open(core.CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.setdefault("out_dir", core.DEFAULT_OUT)
    cfg.setdefault("chat", "")
    cfg.setdefault("limit", 0)
    cfg["api_id"] = api.api_id
    cfg["api_hash"] = api.api_hash
    cfg.pop("phone", None)
    if proxy:
        cfg["proxy"] = proxy
    with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def main():
    ap = argparse.ArgumentParser(description="tdata → 下载器会话转换")
    ap.add_argument("index", nargs="?", type=int, default=None,
                    help="账号索引（0=主账号），默认用 AyuGram 当前活跃账号")
    ap.add_argument("--passcode", default="", help="tdata 本地密码（如设置过）")
    ap.add_argument("--proxy", default="", help="代理地址，如 127.0.0.1:7897")
    ap.add_argument("--tdata", default=os.path.normpath(
        os.path.join(core.APP_DIR, "..", "tdata")), help="tdata 目录")
    args = ap.parse_args()

    try:
        accounts, active = read_accounts(args.tdata, args.passcode.encode())
    except BaseException as e:
        raise SystemExit(
            f"无法解析 tdata（{type(e).__name__}）。"
            "如果给 AyuGram 设置过本地密码，请用 --passcode 提供。"
        )
    if not accounts:
        raise SystemExit("tdata 里没有已登录的账号。")

    print("tdata 中的账号：")
    for a in accounts:
        mark = "（当前活跃）" if a["active"] else ""
        print(f"  [{a['index']}] userId={a['user_id']} DC{a['dc_id']} {mark}")

    idx = args.index if args.index is not None else active
    account = next((a for a in accounts if a["index"] == idx), None)
    if account is None:
        raise SystemExit(f"账号索引 {idx} 不存在或未登录。")

    api = API.TelegramDesktop.Generate()
    print(f"正在为 userId={account['user_id']} 生成会话（api_id={api.api_id}）…")

    # 依次尝试：--proxy 参数 > 系统代理 > 直连
    candidates = []
    if args.proxy:
        host, _, port = args.proxy.rpartition(":")
        candidates.append({"type": "socks5", "host": host, "port": int(port)})
    detected = core.detect_proxy()
    if detected:
        candidates.append(detected)
        print(f"检测到系统代理 {detected['host']}:{detected['port']}，优先尝试…")
    candidates.append(None)

    me = None
    used_proxy = None
    last_err = None
    for proxy in candidates:
        label = f"{proxy['type']} {proxy['host']}:{proxy['port']}" if proxy else "直连"
        try:
            me = await build_session(api, account, proxy)
        except BaseException as e:
            last_err = e
            print(f"  经 {label} 连接失败：{type(e).__name__}")
            continue
        if me is not None:
            used_proxy = proxy
            break
        print(f"  经 {label} 连上了但未授权（密钥可能失效）")
    if me is None:
        raise SystemExit(
            f"会话验证失败（{type(last_err).__name__ if last_err else '未授权'}）。"
            "若你的网络需要代理，请用 --proxy 127.0.0.1:端口 指定。"
        )

    save_config(api, used_proxy)
    print(f"登录成功：{core.display_name(me)}（ID {me.id}）")
    print(f"会话已保存：{core.SESSION_PATH}.session")
    print("配置已写入 config.json —— 现在双击 download_pdfs.bat 即可直接下载。")
    print("提示：Telegram 可能提示“新登录”，这是本工具首次使用该密钥的正常现象。")


if __name__ == "__main__":
    asyncio.run(main())
