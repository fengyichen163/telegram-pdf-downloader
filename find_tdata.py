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
    """解析开始菜单/桌面里 ayugram/telegram 相关 .lnk 的目标 exe。

    用 PowerShell 的 WScript.Shell COM 解析（与资源管理器同源）——自研 MS-SHLLINK
    解析实测会漏（新式 .lnk 的目标只存在 IDList，LinkInfo 里没有）。
    """
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
    scan_dirs = [d for d in scan_dirs if os.path.isdir(d)]
    if not scan_dirs:
        return []

    arr = ",".join("'" + d.replace("'", "''") + "'" for d in scan_dirs)
    cmd = ("$sh = New-Object -ComObject WScript.Shell; "
           f"Get-ChildItem -Path {arr} -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | "
           "ForEach-Object { try { $p = $sh.CreateShortcut($_.FullName).TargetPath; "
           "if ($p -and ($_.Name -match 'ayugram|telegram' -or $p -match 'ayugram|telegram')) "
           "{ $p } } catch {} }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=40,
                             creationflags=_NO_WINDOW).stdout or ""
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines()
            if ln.strip().lower().endswith(".exe") and os.path.isfile(ln.strip())
            and any(k in ln.strip().lower() for k in _EXE_KEYWORDS)]


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
