# -*- coding: utf-8 -*-
"""自动探测本机已登录 Telegram 桌面版 / AyuGram 的 tdata 目录（只读，不改任何东西）。

级联顺序（和 Windows 搜索框找快捷方式同一原理）：
  工具旁 ../tdata → 运行中进程 → 开始菜单/桌面快捷方式(.lnk) → 注册表卸载项 → 已知默认路径
每个候选都用 key_datas 文件存在性验证（convert_tdata 解密的第一步就是读它）。
"""
import os
import subprocess
import sys

KEY_DATAS = "key_datas"
_EXE_KEYWORDS = ("ayugram", "telegram")

# powershell / 子进程不弹黑窗（pythonw 下尤其需要）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def is_valid_tdata(path):
    return bool(path) and os.path.isfile(os.path.join(path, KEY_DATAS))


def find_tdata_dirs():
    """返回去重后的有效 tdata 目录列表，最可能的排在前面。"""
    cands = []

    def add(tdata):
        if not is_valid_tdata(tdata):
            return
        norm = os.path.normpath(os.path.abspath(tdata)).lower()
        if norm not in {os.path.normpath(os.path.abspath(c)).lower() for c in cands}:
            cands.append(os.path.normpath(os.path.abspath(tdata)))

    # 1) 原有约定：tdata 与工具目录并列（D:\...\AyuGram (1)\pdf-downloader 的场景）
    here = os.path.dirname(os.path.abspath(__file__))
    add(os.path.join(os.path.dirname(here), "tdata"))

    # 2) 运行中的 Telegram/AyuGram 进程
    for exe in _running_exe_paths():
        add(os.path.join(os.path.dirname(exe), "tdata"))

    # 3) 开始菜单 / 桌面快捷方式（搜索框能找到的，这里也能找到）
    for exe in _lnk_targets():
        add(os.path.join(os.path.dirname(exe), "tdata"))

    # 4) 注册表卸载项
    for exe in _registry_exe_paths():
        add(os.path.join(os.path.dirname(exe), "tdata"))

    # 5) 已知默认路径（安装版 Telegram Desktop 的 tdata 在 APPDATA，不在 exe 旁）
    add(os.path.join(os.environ.get("APPDATA", ""), "Telegram Desktop", "tdata"))
    add(os.path.join(os.environ.get("LOCALAPPDATA", ""), "AyuGram", "tdata"))

    return cands


def _running_exe_paths():
    """正在运行的 AyuGram/Telegram 进程的 exe 路径（PowerShell，无新依赖）。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process AyuGram,Telegram -ErrorAction SilentlyContinue).Path"],
            capture_output=True, text=True, timeout=20,
            creationflags=_NO_WINDOW).stdout or ""
        return [ln.strip() for ln in out.splitlines()
                if ln.strip().lower().endswith(".exe") and os.path.isfile(ln.strip())]
    except Exception:
        return []


def _lnk_targets():
    """解析开始菜单/桌面里名字含 ayugram/telegram 的 .lnk，返回目标 exe 路径。"""
    if sys.platform != "win32":
        return []
    appdata = os.environ.get("APPDATA", "")
    home = os.path.expanduser("~")
    scan_dirs = [
        os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                     "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(home, "Desktop"),
    ]
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        scan_dirs.append(os.path.join(onedrive, "Desktop"))

    out = []
    for base in scan_dirs:
        if not os.path.isdir(base):
            continue
        for cur, _sub, files in os.walk(base):
            for fn in files:
                low = fn.lower()
                if not low.endswith(".lnk"):
                    continue
                if not any(k in low for k in _EXE_KEYWORDS):
                    continue
                target = _parse_lnk(os.path.join(cur, fn))
                if (target and target.lower().endswith(".exe")
                        and os.path.isfile(target)
                        and any(k in os.path.basename(target).lower() for k in _EXE_KEYWORDS)):
                    out.append(target)
    return out


def _parse_lnk(path):
    """从 .lnk（MS-SHLLINK 格式）提取本地目标路径，失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read(8192)
        if len(data) < 0x4C or data[:4] != b"L\x00\x00\x00":
            return None
        flags = int.from_bytes(data[0x14:0x18], "little")
        if not flags & 0x1:  # HasLinkInfo
            return None
        li = 0x4C  # LinkInfo 紧跟头部
        lbp = int.from_bytes(data[li + 0x10:li + 0x14], "little")  # LocalBasePathOffset
        raw = data[li + lbp:]
        return raw.split(b"\x00", 1)[0].decode("mbcs", "replace") or None
    except OSError:
        return None


def _registry_exe_paths():
    """注册表卸载项里 AyuGram/Telegram 的 exe 路径（便携版常不注册，仅作补充）。"""
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    hives = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in hives:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, sub) as sk:
                        for val in ("DisplayIcon", "InstallLocation"):
                            try:
                                s, _ = winreg.QueryValueEx(sk, val)
                            except OSError:
                                continue
                            s = s.split(",")[0].strip().strip('"')
                            if (s.lower().endswith(".exe") and os.path.isfile(s)
                                    and any(k in os.path.basename(s).lower() for k in _EXE_KEYWORDS)):
                                out.append(s)
                except OSError:
                    pass
    return out


if __name__ == "__main__":
    found = find_tdata_dirs()
    if found:
        print("找到以下有效 tdata：")
        for d in found:
            print("  " + d)
    else:
        print("未找到有效 tdata。")
