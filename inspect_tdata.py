# -*- coding: utf-8 -*-
"""诊断脚本：解析新版 tdata 结构，定位 MTP 授权数据（只输出结构，不输出密钥内容）。"""
import os
import sys

from PyQt5.QtCore import QByteArray, QDataStream
from opentele.td import storage as st
from opentele.td import auth as au

TDATA = sys.argv[1] if len(sys.argv) > 1 else os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tdata"))

KNOWN_TYPES = {v: k for k, v in vars(st.lskType).items()
               if not k.startswith("_") and isinstance(v, int)}
COUNT_TYPES = {0x01, 0x02, 0x03, 0x05, 0x06}
U64_ONLY_TYPES = {
    0x04, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
    0x10, 0x11, 0x12, 0x13, 0x14, 0x16,
}
STICKERS_KEYS = 0x10
MASKS_KEYS = 0x16
SELF_SERIALIZED = 0x15
K_WIDE_IDS_TAG = -1  # Account.kWideIdsTag = ~0


def load_local_key():
    kd = st.Storage.ReadFile("key_data", TDATA)  # ReadFile 自动补 s → key_datas
    salt, key_enc, info_enc = QByteArray(), QByteArray(), QByteArray()
    kd.stream >> salt >> key_enc >> info_enc
    print(f"key_datas: version={kd.version} salt_len={salt.size()} "
          f"keyEnc={key_enc.size()}B infoEnc={info_enc.size()}B")
    passcode_key = st.Storage.CreateLocalKey(salt)
    inner = st.Storage.DecryptLocal(key_enc, passcode_key)
    local_key = au.AuthKey(inner.stream.readRawData(256))
    return local_key, info_enc


def dump_info(info_enc, local_key):
    info = st.Storage.DecryptLocal(info_enc, local_key)
    s = info.stream
    count = s.readInt32()
    indexes = [s.readInt32() for _ in range(min(max(count, 0), 8))]
    trail = []
    while not s.atEnd():
        trail.append(s.readInt32())
    print(f"info: count={count} indexes={indexes} active/trailing={trail}")


def walk_map(name, folder, local_key, passcode=b""):
    try:
        f = st.Storage.ReadFile(name, folder)
    except BaseException as e:
        print(f"  [{name}] 读取失败: {type(e).__name__}")
        return
    legacy_salt, legacy_key_enc, map_enc = QByteArray(), QByteArray(), QByteArray()
    f.stream >> legacy_salt >> legacy_key_enc >> map_enc
    key = local_key
    note = ""
    if legacy_salt.size() > 0 or legacy_key_enc.size() > 0:
        try:
            lk = st.Storage.CreateLegacyLocalKey(legacy_salt, QByteArray(passcode))
            inner = st.Storage.DecryptLocal(legacy_key_enc, lk)
            key = au.AuthKey(inner.stream.readRawData(256))
            note = f"（map 内含独立密钥 salt={legacy_salt.size()}B）"
        except BaseException as e:
            print(f"  [{name}] map 内密钥解密失败（可能设了本地密码）: {type(e).__name__}")
            return
    try:
        m = st.Storage.DecryptLocal(map_enc, key)
    except BaseException as e:
        print(f"  [{name}] 解密失败: {type(e).__name__} {note}")
        return
    print(f"  [{name}] version={f.version} size={m.data.size()}B {note}")
    s = m.stream
    while not s.atEnd():
        t = s.readUInt32()
        tname = KNOWN_TYPES.get(t, f"UNKNOWN_{t:#x}")
        if t in COUNT_TYPES:
            n = s.readUInt32()
            print(f"    {tname}: count={n}")
            for _ in range(n):
                _ = s.readUInt64()
                _ = s.readUInt64()
        elif t == STICKERS_KEYS:
            vals = [s.readUInt64() for _ in range(4)]
            print(f"    {tname}: {[f'{v:016X}' for v in vals]}")
        elif t == MASKS_KEYS:
            vals = [s.readUInt64() for _ in range(3)]
            print(f"    {tname}: {[f'{v:016X}' for v in vals]}")
        elif t == SELF_SERIALIZED:
            ba = QByteArray()
            s >> ba
            print(f"    {tname}: {ba.size()}B")
        elif t in U64_ONLY_TYPES:
            k = s.readUInt64()
            print(f"    {tname}: {k:016X}")
        else:
            raw = bytes(m.data)
            pos = s.device().pos()
            print(f"    {tname}: 停止，剩余 {len(raw) - pos} 字节")
            break


def read_mtp_file(data_name_key_str, local_key):
    """解析根目录 <dataNameKey>s（MTP 授权数据），blockId 应为 75。"""
    try:
        f = st.Storage.ReadEncryptedFile(data_name_key_str, TDATA, local_key)
    except BaseException as e:
        print(f"  {data_name_key_str}s: 读取/解密失败: {type(e).__name__}")
        return
    s = f.stream
    block = s.readInt32()
    print(f"  {data_name_key_str}s: version={f.version} blockId={block}")
    if block != 75:
        return
    ba = QByteArray()
    s >> ba
    print(f"    mtp blob: {ba.size()}B")
    ms = QDataStream(ba)
    ms.setVersion(QDataStream.Version.Qt_5_1)
    user_id = ms.readInt32()
    dc_id = ms.readInt32()
    if ((user_id << 32) | dc_id) & 0xFFFFFFFFFFFFFFFF == K_WIDE_IDS_TAG & 0xFFFFFFFFFFFFFFFF:
        user_id = ms.readUInt64()
        dc_id = ms.readInt32()
    print(f"    userId={user_id} mainDcId={dc_id}")

    def read_keys(label):
        n = ms.readInt32()
        print(f"    {label}: count={n}")
        out = []
        for _ in range(max(0, min(n, 8))):
            kdc = ms.readInt32()
            data = bytes(ms.readRawData(256))
            print(f"      dcId={kdc} keyLen={len(data)}B sha1[:8]={'' if len(data) < 256 else __import__('hashlib').sha1(data).hexdigest()[:8]}")
            out.append((kdc, data))
        return out

    keys = read_keys("mtpKeys")
    to_destroy = read_keys("mtpKeysToDestroy")
    rest = ba.size() - ms.device().pos()
    print(f"    剩余 {rest} 字节")
    return user_id, dc_id, keys


def main():
    local_key, info_enc = load_local_key()
    print("localKey: 已解出（不显示内容）")
    dump_info(info_enc, local_key)

    print("--- 账号目录名推导 ---")
    folders = [f for f in os.listdir(TDATA)
               if os.path.isdir(os.path.join(TDATA, f)) and len(f) == 16]
    for base in ("data", "data#2", "data#3"):
        fn = st.Storage.ToFilePart(st.Storage.ComputeDataNameKey(base))
        print(f"  {base!r} -> {fn} {'✓' if fn in folders else '✗'}")

    # 根目录 MTP 授权文件：<账号目录名>s
    print("--- 根目录 MTP 授权文件 ---")
    for folder in folders:
        read_mtp_file(folder, local_key)

    for folder in folders:
        path = os.path.join(TDATA, folder)
        print(f"== {folder}/ ==")
        walk_map("map", path, local_key)


if __name__ == "__main__":
    main()
