# -*- coding: utf-8 -*-
"""Telegram 群/频道 文件一键下载器（GUI 版）。

- 扫描群/频道全部历史消息，列出所有文件（文档/视频/音频/图片）
- 按格式筛选（如 pdf,epub,zip）、按文件名搜索、勾选下载
- 多连接并行下载、.part 断点续传
- 首次使用需要 api_id / api_hash，或直接用 convert_tdata.py 复用 AyuGram 登录
"""
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core


class Bridge:
    """GUI 与后台线程之间传递验证码/密码的桥。"""
    def __init__(self):
        self.code_value = None
        self.password_value = None
        self.code_event = threading.Event()
        self.password_event = threading.Event()


class LoginWorker(threading.Thread):
    """登录/会话检查。interactive=False 时只检查不主动要验证码。"""

    def __init__(self, api_id, api_hash, phone, q, bridge, interactive):
        super().__init__(daemon=True)
        self.api_id, self.api_hash, self.phone = api_id, api_hash, phone
        self.q, self.bridge, self.interactive = q, bridge, interactive

    def run(self):
        try:
            state = self._main()
            if state == "authed":
                self.q.put(("authed", None))
            else:
                self.q.put(("not_authed", None))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("error", f"登录失败：{e}{core.connection_hint(e)}"))

    def _main(self):
        q = self.q

        def log(t):
            q.put(("log", t))

        async def run():
            async with core.connected_client(core.SESSION_PATH, self.api_id, self.api_hash) as client:
                if await client.is_user_authorized():
                    me = await client.get_me()
                    log(f"已登录：{core.display_name(me)}（会话已保存，之后无需重复登录）")
                    return "authed"
                if not self.interactive:
                    log("尚未登录：请填好 API ID / API HASH / 手机号，点【保存并登录】。")
                    return "not_authed"

                def ask_code():
                    q.put(("need_code", None))
                    self.bridge.code_event.clear()
                    if not self.bridge.code_event.wait(300):
                        raise TimeoutError("等待验证码超时，请重新点【保存并登录】。")
                    return (self.bridge.code_value or "").strip().replace(" ", "")

                def ask_password():
                    q.put(("need_password", None))
                    self.bridge.password_event.clear()
                    if not self.bridge.password_event.wait(300):
                        raise TimeoutError("等待密码超时，请重新登录。")
                    return self.bridge.password_value or ""

                me = await core.login_flow(client, self.phone,
                                           log=log, ask_code=ask_code,
                                           ask_password=ask_password)
                log(f"登录成功：{core.display_name(me)}")
                return "authed"

        import asyncio
        return asyncio.run(run())


class ScanWorker(threading.Thread):
    def __init__(self, api_id, api_hash, chat, limit, q):
        super().__init__(daemon=True)
        self.api_id, self.api_hash = api_id, api_hash
        self.chat, self.limit, self.q = chat, limit, q
        self.cancel = False

    def run(self):
        try:
            import asyncio
            entries = asyncio.run(core.run_scan(
                self.api_id, self.api_hash, self.chat, self.limit,
                log=lambda t: self.q.put(("log", t)),
                on_scan=lambda n: self.q.put(("scan", n)),
                cancelled=lambda: self.cancel,
            ))
            self.q.put(("scanned", entries))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("error", f"扫描出错：{e}{core.connection_hint(e)}"))


class DownloadWorker(threading.Thread):
    def __init__(self, api_id, api_hash, chat, entries, out_dir, workers, q):
        super().__init__(daemon=True)
        self.api_id, self.api_hash = api_id, api_hash
        self.chat, self.entries = chat, entries
        self.out_dir, self.workers, self.q = out_dir, workers, q
        self.cancel = False

    def run(self):
        try:
            import asyncio
            ok, skip, fail = asyncio.run(core.download_entries(
                self.api_id, self.api_hash, self.entries, self.out_dir,
                chat_input=self.chat,
                log=lambda t: self.q.put(("log", t)),
                on_found=lambda n, tb: self.q.put(("found", n, tb)),
                on_progress=lambda df, tf, bd, tb, name: self.q.put(
                    ("dl", df, tf, bd, tb, name)),
                cancelled=lambda: self.cancel,
                workers=self.workers,
            ))
            self.q.put(("done", ok, skip, fail))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("error", f"下载出错：{e}{core.connection_hint(e)}"))


class TdataDetectWorker(threading.Thread):
    """探测本机已登录的 tdata 目录（find_tdata 级联）。"""

    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q

    def run(self):
        try:
            import find_tdata
            self.q.put(("tdata_found", find_tdata.find_tdata_dirs()))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("error", f"检测 tdata 失败：{e}"))


class TdataConvertWorker(threading.Thread):
    """子进程跑 convert_tdata.py，输出逐行回传 GUI 日志。"""

    def __init__(self, q, tdata=None):
        super().__init__(daemon=True)
        self.q, self.tdata = q, tdata

    def run(self):
        try:
            # 转换依赖 opentele（含 PyQt5）；下载器首次运行只装了 telethon
            r = subprocess.run([sys.executable, "-c", "import opentele"],
                               capture_output=True, creationflags=0x08000000)
            if r.returncode != 0:
                self.q.put(("log", "首次使用：正在安装转换依赖 opentele（含 PyQt5，约几十 MB）…"))
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "opentele"],
                               check=True, creationflags=0x08000000)
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            cmd = [sys.executable, "convert_tdata.py"]
            if self.tdata:
                cmd += ["--tdata", self.tdata]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    env=env, cwd=core.APP_DIR, creationflags=0x08000000)
            for line in proc.stdout:
                self.q.put(("log", line.rstrip()))
            self.q.put(("tdata_convert_done", proc.wait() == 0))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("tdata_convert_done", False))
            self.q.put(("error", f"tdata 转换失败：{e}"))


TG_PORTABLE_URL = "https://telegram.org/dl/desktop/win64"


class TdataDownloadWorker(threading.Thread):
    """下载 Telegram 官方便携版 zip（走系统代理），解压并启动，让用户先登录一次。"""

    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q

    def run(self):
        try:
            import io
            import urllib.request
            import zipfile
            handlers = []
            p = core.detect_proxy()
            if p:
                # Clash mixed 端口同时接受 http 代理协议，无需 socks 支持
                proxy = f"http://{p['host']}:{p['port']}"
                handlers.append(urllib.request.ProxyHandler(
                    {"http": proxy, "https": proxy}))
            opener = urllib.request.build_opener(*handlers)
            self.q.put(("log", "正在下载 Telegram 官方便携版（约 60 MB）…"))
            resp = opener.open(TG_PORTABLE_URL, timeout=60)
            total = int(resp.headers.get("Content-Length") or 0)
            buf, last = bytearray(), 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                buf += chunk
                if total and len(buf) - last >= 4 * 1024 * 1024:
                    last = len(buf)
                    self.q.put(("log", f"已下载 {core.human_size(len(buf))}"
                                       f" / {core.human_size(total)}"))
            dest = os.path.join(core.APP_DIR, "Telegram")
            with zipfile.ZipFile(io.BytesIO(bytes(buf))) as z:
                z.extractall(dest)
            exe = next((os.path.join(cur, fn)
                        for cur, _s, fs in os.walk(dest)
                        for fn in fs if fn.lower() == "telegram.exe"), None)
            if not exe:
                raise RuntimeError("解压后没找到 Telegram.exe")
            os.startfile(exe)
            self.q.put(("tdata_dl_done", True, exe))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("tdata_dl_done", False, str(e)))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Telegram 文件下载器（群/频道 · 全类型 · 可筛选勾选）")
        root.geometry("960x780")
        root.minsize(820, 640)

        self.q = queue.Queue()
        self.bridge = Bridge()
        self.worker = None
        self.mode = None  # None / "login" / "scan" / "download"
        self.authed = False
        self.entries = []       # 扫描出的全部文件条目
        self.visible = []       # 当前筛选条件下显示的条目下标
        self.checked = set()    # 勾选的条目下标

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)

        # ---------- ① 登录区 ----------
        lf = ttk.LabelFrame(main, text="① 登录（首次使用需配置，之后自动记住）", padding=6)
        lf.pack(fill="x")
        lf.columnconfigure(1, weight=1)

        ttk.Label(lf, text="API ID:").grid(row=0, column=0, sticky="e")
        self.api_id_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.api_id_var, width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(lf, text="API HASH:").grid(row=1, column=0, sticky="e")
        self.api_hash_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.api_hash_var).grid(row=1, column=1, columnspan=2, sticky="we")
        ttk.Label(lf, text="手机号:").grid(row=2, column=0, sticky="e")
        self.phone_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.phone_var, width=18).grid(row=2, column=1, sticky="w")
        ttk.Label(lf, text="（国际格式，如 +8613800138000；用 convert_tdata 转换过则不用管）",
                  foreground="#888").grid(row=2, column=2, sticky="w")
        ttk.Label(lf, text="代理:").grid(row=3, column=0, sticky="e")
        self.proxy_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.proxy_var, width=18).grid(row=3, column=1, sticky="w")
        ttk.Label(lf, text="（host:port，如 127.0.0.1:7897；留空=直连）", foreground="#888").grid(row=3, column=2, sticky="w")

        ttk.Label(lf, foreground="#666", wraplength=880, justify="left", text=(
            "配置教程（三选一，只需一次）：\n"
            "① 推荐：点【一键本地登录】，自动找到本机已登录的 Telegram 桌面版 / AyuGram 的 tdata，"
            "免申请、免验证码。也可在命令行运行 python convert_tdata.py（自动探测 tdata，"
            "找不到时用 --tdata \"路径\" 指定，或用 login.bat 一键完成）。\n"
            "② 手动：浏览器打开 https://my.telegram.org → 用手机号登录 → API development tools → "
            "随便填个应用标题创建 → 把 api_id 和 api_hash 复制到上面，再点【保存并登录】收验证码。"
        )).grid(row=4, column=0, columnspan=3, sticky="we", pady=(4, 0))

        lrow = ttk.Frame(lf)
        lrow.grid(row=5, column=0, columnspan=3, sticky="we", pady=4)
        self.login_btn = ttk.Button(lrow, text="保存并登录", command=self.on_login)
        self.login_btn.pack(side="left")
        self.tdata_btn = ttk.Button(lrow, text="一键本地登录", command=self.on_tdata_login)
        self.tdata_btn.pack(side="left", padx=(8, 0))
        self.auth_status = ttk.Label(lrow, text="未检查", foreground="#888")
        self.auth_status.pack(side="left", padx=8)

        vrow = ttk.Frame(lf)
        vrow.grid(row=6, column=0, columnspan=3, sticky="we")
        ttk.Label(vrow, text="验证码:").pack(side="left")
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(vrow, textvariable=self.code_var, width=10)
        self.code_entry.pack(side="left", padx=4)
        ttk.Button(vrow, text="提交验证码", command=self.on_submit_code).pack(side="left")
        ttk.Label(vrow, text="    两步验证密码:").pack(side="left")
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(vrow, textvariable=self.pw_var, width=14, show="•")
        self.pw_entry.pack(side="left", padx=4)
        ttk.Button(vrow, text="提交密码", command=self.on_submit_password).pack(side="left")

        # ---------- ② 扫描 ----------
        sf = ttk.LabelFrame(main, text="② 扫描群/频道文件（文档/视频/音频/图片）", padding=6)
        sf.pack(fill="x", pady=(6, 0))
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text="群/频道:").grid(row=0, column=0, sticky="e")
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(sf, textvariable=self.chat_var)
        self.chat_entry.grid(row=0, column=1, sticky="we")
        ttk.Label(sf, text="@用户名 / t.me/链接 / 数字ID", foreground="#888").grid(row=0, column=2, sticky="w")

        ttk.Label(sf, text="保存到:").grid(row=1, column=0, sticky="e")
        self.out_var = tk.StringVar(value=core.DEFAULT_OUT)
        ttk.Entry(sf, textvariable=self.out_var).grid(row=1, column=1, sticky="we")
        ttk.Button(sf, text="浏览…", command=self.on_browse).grid(row=1, column=2, sticky="w", padx=(4, 0))

        ttk.Label(sf, text="扫描条数:").grid(row=2, column=0, sticky="e")
        slim = ttk.Frame(sf)
        slim.grid(row=2, column=1, sticky="w")
        self.limit_var = tk.IntVar(value=0)
        ttk.Spinbox(slim, from_=0, to=10 ** 9, textvariable=self.limit_var, width=9).pack(side="left")
        ttk.Label(slim, text="  0=全部    并行连接:", foreground="#888").pack(side="left")
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(slim, from_=1, to=8, textvariable=self.workers_var, width=4).pack(side="left")
        ttk.Label(slim, text="（1~8）", foreground="#888").pack(side="left")

        srow = ttk.Frame(sf)
        srow.grid(row=3, column=0, columnspan=3, sticky="we", pady=(5, 0))
        self.scan_btn = ttk.Button(srow, text="扫描文件列表", command=self.on_scan, state="disabled")
        self.scan_btn.pack(side="left")

        # ---------- ③ 筛选 + 列表 + 下载 ----------
        df = ttk.LabelFrame(main, text="③ 筛选 / 勾选 / 下载", padding=6)
        df.pack(fill="both", expand=True, pady=(6, 0))
        df.columnconfigure(0, weight=1)
        df.rowconfigure(2, weight=1)

        frow = ttk.Frame(df)
        frow.grid(row=0, column=0, sticky="we")
        ttk.Label(frow, text="格式:").pack(side="left")
        self._ext_map = {"全部格式": None}
        self.ext_var = tk.StringVar(value="全部格式")
        self.ext_box = ttk.Combobox(frow, textvariable=self.ext_var, width=14,
                                    state="readonly", values=["全部格式"])
        self.ext_box.pack(side="left", padx=(2, 8))
        self.ext_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_view())
        ttk.Label(frow, text="搜索:").pack(side="left")
        self.search_var = tk.StringVar()
        search_e = ttk.Entry(frow, textvariable=self.search_var, width=24)
        search_e.pack(side="left", padx=(2, 8))
        ttk.Label(frow, text="（扫描后下拉可选频道里出现过的格式；搜索实时生效）",
                  foreground="#888").pack(side="left")
        search_e.bind("<KeyRelease>", lambda e: self.refresh_view())

        self.stats_var = tk.StringVar(value="尚未扫描。先点【扫描文件列表】。")
        ttk.Label(df, textvariable=self.stats_var, foreground="#333").grid(row=1, column=0, sticky="w")

        cols = ("sel", "ext", "size", "date", "name")
        self.tv = ttk.Treeview(df, columns=cols, show="headings", height=11)
        for cid, text, w in (("sel", "选", 36), ("ext", "格式", 60),
                             ("size", "大小", 90), ("date", "日期", 90), ("name", "文件名", 520)):
            self.tv.heading(cid, text=text)
            self.tv.column(cid, width=w, minwidth=40,
                           stretch=(cid == "name"))
        ys = ttk.Scrollbar(df, command=self.tv.yview)
        self.tv.configure(yscrollcommand=ys.set)
        self.tv.grid(row=2, column=0, sticky="nsew")
        ys.grid(row=2, column=1, sticky="ns")
        self.tv.bind("<Button-1>", self.on_tree_click)

        brow = ttk.Frame(df)
        brow.grid(row=3, column=0, columnspan=2, sticky="we", pady=(5, 0))
        ttk.Button(brow, text="全选(筛选结果)", command=lambda: self.select_visible("all")).pack(side="left")
        ttk.Button(brow, text="反选(筛选结果)", command=lambda: self.select_visible("invert")).pack(side="left", padx=4)
        ttk.Button(brow, text="清除勾选", command=lambda: self.select_visible("none")).pack(side="left")
        self.dl_sel_btn = ttk.Button(brow, text="下载勾选的文件", command=self.on_download_checked, state="disabled")
        self.dl_sel_btn.pack(side="left", padx=(14, 4))
        self.dl_vis_btn = ttk.Button(brow, text="下载全部筛选结果", command=self.on_download_filtered, state="disabled")
        self.dl_vis_btn.pack(side="left")
        self.cancel_btn = ttk.Button(brow, text="取消", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="right")

        self.progress = ttk.Progressbar(df, maximum=100)
        self.progress.grid(row=4, column=0, columnspan=2, sticky="we", pady=(6, 2))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(df, textvariable=self.status_var, foreground="#333").grid(row=5, column=0, columnspan=2, sticky="w")

        # ---------- 日志 ----------
        lf2 = ttk.LabelFrame(main, text="日志", padding=4)
        lf2.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(lf2, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        lys = ttk.Scrollbar(lf2, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lys.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lys.pack(side="right", fill="y")

        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)
        self._append_log("提示：扫描 → 用格式/搜索筛选 → 点文件行勾选（或全选）→ 下载勾选的文件。")
        if ((self.api_id_var.get() and self.api_hash_var.get())
                or os.path.exists(core.SESSION_PATH + ".session")):
            self._start_login(interactive=False)  # 启动时静默检查已保存的会话

    # ---------- 配置 ----------
    def _load_config(self):
        try:
            with open(core.CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        self.api_id_var.set(str(cfg.get("api_id", "") or ""))
        self.api_hash_var.set(cfg.get("api_hash", "") or "")
        self.phone_var.set(cfg.get("phone", "") or "")
        self.out_var.set(cfg.get("out_dir") or core.DEFAULT_OUT)
        self.chat_var.set(cfg.get("chat", "") or "")
        try:
            self.limit_var.set(int(cfg.get("limit", 0) or 0))
        except (TypeError, ValueError):
            pass
        try:
            self.workers_var.set(int(cfg.get("workers", 4) or 4))
        except (TypeError, ValueError):
            self.workers_var.set(4)
        p = cfg.get("proxy") or {}
        if p.get("host"):
            self.proxy_var.set(f"{p['host']}:{p.get('port', '')}")
        elif not self.proxy_var.get():
            detected = core.detect_proxy()
            if detected:
                self.proxy_var.set(f"{detected['host']}:{detected['port']}")

    def _save_config(self):
        # 先读旧配置做合并，避免弄丢 convert_tdata.py 等写入的字段（如 proxy）
        try:
            with open(core.CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        # api_id/api_hash/phone 空值时保留旧值：tdata 转换写入的 api 参数不在界面上，
        # 不能被一次空输入框的保存冲掉（否则启动静默检查不跑，显示"未检查"）
        cfg["api_id"] = self.api_id_var.get().strip() or cfg.get("api_id", "")
        cfg["api_hash"] = self.api_hash_var.get().strip() or cfg.get("api_hash", "")
        cfg["phone"] = self.phone_var.get().strip() or cfg.get("phone", "")
        cfg["out_dir"] = self.out_var.get().strip()
        cfg["chat"] = self.chat_var.get().strip()
        cfg["limit"] = self.limit_var.get()
        cfg["workers"] = self.workers_var.get()
        try:
            proxy = core.parse_proxy_text(self.proxy_var.get())
        except ValueError as e:
            messagebox.showerror("提示", str(e))
            return False
        if proxy:
            cfg["proxy"] = proxy
        else:
            cfg.pop("proxy", None)
        try:
            with open(core.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return True

    # ---------- 登录 ----------
    def on_login(self):
        self._start_login(interactive=True)

    def on_tdata_login(self):
        """一键本地登录：探测 tdata → 转换，找不到时给三分支选择。"""
        if self.mode:
            return
        self._set_busy("login")
        self._append_log("正在探测本机已登录的 tdata（进程/快捷方式/注册表）…")
        TdataDetectWorker(self.q).start()

    def _handle_tdata_found(self, dirs):
        self._set_idle()
        if dirs:
            self._append_log("检测到 " + "；".join(dirs))
            if len(dirs) == 1:
                self._run_tdata_convert(dirs[0])
            else:
                pick = self._ask_choose(dirs)
                if pick:
                    self._run_tdata_convert(pick)
            return
        act = self._ask_tdata_action()
        if act == "manual":
            d = filedialog.askdirectory(
                title="选择 Telegram 桌面版 / AyuGram 的 tdata 目录",
                initialdir=self.out_var.get() or core.DEFAULT_OUT)
            if d:
                import find_tdata
                if find_tdata.is_valid_tdata(d):
                    self._run_tdata_convert(d)
                else:
                    messagebox.showerror(
                        "提示", "该目录里没有 key_datas，不像已登录的 tdata。\n"
                        "请确认选择的是 tdata 目录本身（里面应有 key_datas、D877… 等文件）。")
        elif act == "install":
            self._set_busy("login")
            self._append_log("下载并安装 Telegram 官方便携版（便携版 tdata 就在程序目录里）…")
            TdataDownloadWorker(self.q).start()
        elif act == "code":
            self._append_log("请按上方教程②填好 API ID / API HASH / 手机号，点【保存并登录】收验证码。")

    def _run_tdata_convert(self, tdata):
        self._set_busy("login")
        self._append_log(f"开始从 tdata 转换登录（{tdata}）…")
        TdataConvertWorker(self.q, tdata).start()

    def _ask_tdata_action(self):
        """没找到 tdata 时的三分支选择，返回 'manual'/'install'/'code' 或 None。"""
        win = tk.Toplevel(self.root)
        win.title("一键本地登录")
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, padding=12, wraplength=420, justify="left", text=(
            "没有在本机检测到已登录的 tdata。\n"
            "（要求 Telegram 桌面版 / AyuGram 已安装且登录过一次）")).pack()
        result = []

        def done(v):
            result.append(v)
            win.destroy()

        row = ttk.Frame(win, padding=(12, 0, 12, 12))
        row.pack()
        ttk.Button(row, text="手动选择\ntdata 目录", width=14,
                   command=lambda: done("manual")).pack(side="left", padx=4)
        ttk.Button(row, text="帮我装 Telegram\n便携版", width=14,
                   command=lambda: done("install")).pack(side="left", padx=4)
        ttk.Button(row, text="改用验证码登录\n（填 api 参数）", width=14,
                   command=lambda: done("code")).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        self.root.wait_window(win)
        return result[0] if result else None

    def _ask_choose(self, dirs):
        """多个候选 tdata 时让用户选一个。"""
        win = tk.Toplevel(self.root)
        win.title("选择 tdata")
        win.transient(self.root)
        ttk.Label(win, text="检测到多个已登录的 tdata，请选择：", padding=10).pack(anchor="w")
        var = tk.StringVar(value=dirs[0])
        box = ttk.Frame(win)
        box.pack(fill="x", padx=16)
        for d in dirs:
            ttk.Radiobutton(box, text=d, value=d, variable=var).pack(anchor="w", pady=2)
        pick = []

        def ok():
            pick.append(var.get())
            win.destroy()

        ttk.Button(win, text="确定", command=ok).pack(pady=10)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        self.root.wait_window(win)
        return pick[0] if pick else None

    def _start_login(self, interactive):
        if self.mode:
            return
        api_id_text = self.api_id_var.get().strip()
        api_hash = self.api_hash_var.get().strip() or None
        phone = self.phone_var.get().strip()
        if interactive:
            try:
                api_id = int(api_id_text)
            except ValueError:
                messagebox.showerror("提示", "API ID 必须是数字（在 my.telegram.org 获取）。")
                return
            if not api_hash or not phone:
                messagebox.showerror("提示", "请填写 API HASH 和手机号。")
                return
        else:
            # 静默检查（启动/转换后）：tdata 转换的会话没有手机号也有效；
            # api 参数留空时由 make_client 回退读 config.json
            try:
                api_id = int(api_id_text) if api_id_text else None
            except ValueError:
                api_id = None
        if not self._save_config():
            return
        self._set_busy("login")
        self.worker = LoginWorker(api_id, api_hash, phone, self.q, self.bridge, interactive)
        self.worker.start()

    def on_submit_code(self):
        self.bridge.code_value = self.code_var.get()
        self.bridge.code_event.set()
        self._append_log("验证码已提交…")

    def on_submit_password(self):
        self.bridge.password_value = self.pw_var.get()
        self.bridge.password_event.set()
        self._append_log("密码已提交…")

    # ---------- 扫描 ----------
    def on_scan(self):
        if self.mode or not self.authed:
            return
        chat = self.chat_var.get().strip()
        if not chat:
            messagebox.showerror("提示", "请填写群/频道链接，例如 @durov 或 t.me/durov。")
            return
        try:
            api_id = int(self.api_id_var.get().strip())
            limit = int(self.limit_var.get())
        except ValueError:
            messagebox.showerror("提示", "API ID / 扫描条数必须是数字。")
            return
        if not self._save_config():
            return
        self.entries, self.checked, self.visible = [], set(), []
        self.refresh_view()
        self._set_busy("scan")
        self.worker = ScanWorker(api_id, self.api_hash_var.get().strip(), chat, limit, self.q)
        self.worker.start()

    # ---------- 列表/筛选 ----------
    def refresh_view(self):
        ext = self._ext_map.get(self.ext_var.get())
        exts = [ext] if ext else None
        search = self.search_var.get().strip()
        self.visible = [i for i, e in enumerate(self.entries)
                        if core.filter_entries([e], exts, search or None)]
        self.tv.delete(*self.tv.get_children())
        for i in self.visible:
            e = self.entries[i]
            self.tv.insert("", "end", iid=str(i), values=(
                "✓" if i in self.checked else "", e["ext"], core.human_size(e["size"]),
                e["date"], e["name"]))
        self._update_stats()

    def _rebuild_ext_box(self):
        """扫描后按频道里实际出现的格式重建下拉项（带数量，按数量降序）。"""
        counts = {}
        for e in self.entries:
            ext = (e["ext"] or "bin").lower()
            counts[ext] = counts.get(ext, 0) + 1
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        self._ext_map = {"全部格式": None}
        vals = ["全部格式"]
        for ext, c in items:
            label = f"{ext} ({c})"
            self._ext_map[label] = ext
            vals.append(label)
        self.ext_box.config(values=vals)
        self.ext_var.set("全部格式")

    def on_tree_click(self, event):
        row = self.tv.identify_row(event.y)
        if not row:
            return
        idx = int(row)
        if idx in self.checked:
            self.checked.discard(idx)
        else:
            self.checked.add(idx)
        self.tv.set(row, "sel", "✓" if idx in self.checked else "")
        self._update_stats()
        return "break"

    def select_visible(self, how):
        if how == "all":
            self.checked.update(self.visible)
        elif how == "invert":
            self.checked ^= set(self.visible)
        else:
            self.checked -= set(self.visible)
        for i in self.visible:
            self.tv.set(str(i), "sel", "✓" if i in self.checked else "")
        self._update_stats()

    def _update_stats(self):
        if not self.entries:
            return
        vis = [self.entries[i] for i in self.visible]
        chk = [self.entries[i] for i in self.checked if i in set(self.visible)]
        vb = sum(e["size"] for e in vis)
        cb = sum(e["size"] for e in chk)
        self.stats_var.set(
            f"筛选结果: {len(vis)} 个 / {core.human_size(vb)}    "
            f"已勾选(筛选内): {len(chk)} 个 / {core.human_size(cb)}    "
            f"总计: {len(self.entries)} 个 / {core.human_size(sum(e['size'] for e in self.entries))}")

    # ---------- 下载 ----------
    def _start_download(self, entries):
        if self.mode or not entries:
            messagebox.showinfo("提示", "没有可下载的文件（先扫描，再筛选/勾选）。")
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("提示", "请选择保存目录。")
            return
        try:
            api_id = int(self.api_id_var.get().strip())
        except ValueError:
            messagebox.showerror("提示", "API ID 必须是数字。")
            return
        if not self._save_config():
            return
        self._set_busy("download")
        self.worker = DownloadWorker(api_id, self.api_hash_var.get().strip(),
                                     self.chat_var.get().strip(), entries, out,
                                     self.workers_var.get(), self.q)
        self.worker.start()

    def on_download_checked(self):
        self._start_download([self.entries[i] for i in sorted(self.checked)])

    def on_download_filtered(self):
        self._start_download([self.entries[i] for i in self.visible])

    def on_cancel(self):
        if self.worker is not None:
            self.worker.cancel = True
            self._append_log("正在取消…（当前文件处理完后停止）")

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or core.DEFAULT_OUT)
        if d:
            self.out_var.set(d)

    def on_open_dir(self):
        import os
        d = self.out_var.get().strip()
        if d and os.path.isdir(d):
            os.startfile(d)

    # ---------- 状态 ----------
    def _set_busy(self, mode):
        self.mode = mode
        self.cancel_btn.config(state="enabled" if mode in ("scan", "download") else "disabled")
        self.login_btn.config(state="enabled" if mode is None else "disabled")
        self.tdata_btn.config(state="enabled" if mode is None else "disabled")
        self.scan_btn.config(state="enabled" if (mode is None and self.authed) else "disabled")
        for b in (self.dl_sel_btn, self.dl_vis_btn):
            b.config(state="enabled" if (mode is None and self.entries) else "disabled")
        if mode == "login":
            self.status_var.set("正在登录/检查会话…")
        elif mode == "scan":
            self.status_var.set("扫描中…（消息多时需要几分钟）")
            self.progress.config(value=0)
        else:
            self.status_var.set("准备下载…")
            self.progress.config(value=0)

    def _set_idle(self):
        self.mode = None
        self.cancel_btn.config(state="disabled")
        self.login_btn.config(state="enabled")
        self.tdata_btn.config(state="enabled")
        self.scan_btn.config(state="enabled" if self.authed else "disabled")
        for b in (self.dl_sel_btn, self.dl_vis_btn):
            b.config(state="enabled" if (self.authed and self.entries) else "disabled")

    def _append_log(self, text):
        core.log_file(text)
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, item):
        kind = item[0]
        if kind == "log":
            self._append_log(item[1])
        elif kind == "scan":
            self.status_var.set(f"已扫描 {item[1]} 条消息…")
        elif kind == "scanned":
            self.entries = item[1]
            self.checked = set()
            self._rebuild_ext_box()
            self.refresh_view()
            self._set_idle()
            self._append_log(f"扫描结束：共 {len(self.entries)} 个文件。可筛选后下载。")
        elif kind == "found":
            n, total_bytes = item[1], item[2]
            self.progress.config(value=0)
            self.status_var.set(f"待下载 {n} 个文件（{core.human_size(total_bytes)}），并行下载中…")
        elif kind == "dl":
            df, tf, bd, tb, name = item[1:]
            pct = bd / tb * 100 if tb else 100
            self.progress.config(value=min(pct, 100))
            self.status_var.set(
                f"[{df}/{tf}] {core.human_size(bd)} / {core.human_size(tb)} · {name}")
        elif kind == "done":
            ok, skip, fail = item[1:]
            self._append_log(f"下载结束：成功 {ok}，跳过 {skip}，失败 {fail}")
            self.status_var.set(f"完成：成功 {ok}，跳过 {skip}，失败 {fail}")
            if ok:
                self.progress.config(value=100)
            self._set_idle()
        elif kind == "error":
            self._append_log(item[1])
            messagebox.showerror("出错了", item[1])
            self._set_idle()
        elif kind == "tdata_found":
            self._handle_tdata_found(item[1])
        elif kind == "tdata_convert_done":
            ok = item[1]
            self._set_idle()
            if ok:
                self._append_log("tdata 转换完成，正在检查登录状态…")
                self._load_config()
                self._start_login(interactive=False)
            else:
                messagebox.showerror("出错了", "tdata 转换失败，详见日志。")
        elif kind == "tdata_dl_done":
            ok, msg = item[1], item[2]
            self._set_idle()
            if ok:
                messagebox.showinfo(
                    "提示", "Telegram 便携版已下载并启动。\n"
                    "请先在弹出的 Telegram 里登录你的账号，\n"
                    "然后回到本工具再点一次【一键本地登录】即可。")
            else:
                messagebox.showerror(
                    "出错了", f"下载失败：{msg}\n"
                    "可手动从 telegram.org 下载便携版解压后，用【手动选择 tdata 目录】重试。")
        elif kind == "need_code":
            self._append_log("请输入验证码，然后点【提交验证码】。")
            self.code_entry.focus_set()
        elif kind == "need_password":
            self._append_log("请输入两步验证密码，然后点【提交密码】。")
            self.pw_entry.focus_set()
        elif kind == "authed":
            self.authed = True
            self.auth_status.config(text="已登录 ✔", foreground="#2a7")
            self._set_idle()
        elif kind == "not_authed":
            self.authed = False
            self.auth_status.config(text="未登录", foreground="#a66")
            self._set_idle()

    def on_close(self):
        self._save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
