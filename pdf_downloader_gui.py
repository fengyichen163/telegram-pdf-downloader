# -*- coding: utf-8 -*-
"""Telegram 群/频道 PDF 一键下载器（GUI 版）。

- 用自己的 Telegram 账号登录（MTProto），遍历指定群/频道的历史消息，
  找出所有 PDF 并批量下载到本地。
- 首次使用需要 api_id / api_hash：到 https://my.telegram.org 免费申请（只需一次）。
- 会话保存在本目录 session_downloader.session，登录一次后以后直接用。
"""
import json
import queue
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
            async with core.make_client(core.SESSION_PATH, self.api_id, self.api_hash) as client:
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

        return __import__("asyncio").run(run())


class DownloadWorker(threading.Thread):
    def __init__(self, api_id, api_hash, chat, out_dir, limit, q):
        super().__init__(daemon=True)
        self.api_id, self.api_hash = api_id, api_hash
        self.chat, self.out_dir, self.limit, self.q = chat, out_dir, limit, q
        self.cancel = False

    def run(self):
        try:
            import asyncio
            ok, skip, fail = asyncio.run(core.run_download(
                self.api_id, self.api_hash, self.chat, self.out_dir, self.limit,
                log=lambda t: self.q.put(("log", t)),
                on_scan=lambda n: self.q.put(("scan", n)),
                on_found=lambda n: self.q.put(("found", n)),
                on_dl=lambda i, n, name, cur, tot: self.q.put(("dl", i, n, name, cur, tot)),
                cancelled=lambda: self.cancel,
            ))
            self.q.put(("done", ok, skip, fail))
        except Exception as e:
            core.log_file(traceback.format_exc())
            self.q.put(("error", f"下载出错：{e}{core.connection_hint(e)}"))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Telegram PDF 一键下载器（群/频道批量下载）")
        root.geometry("800x700")
        root.minsize(700, 580)

        self.q = queue.Queue()
        self.bridge = Bridge()
        self.worker = None
        self.mode = None  # None / "login" / "download"
        self.authed = False

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # ---------- ① 登录区 ----------
        lf = ttk.LabelFrame(main, text="① 登录（首次使用需配置，之后自动记住）", padding=8)
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
        ttk.Label(lf, text="（国际格式，如 +8613800138000）", foreground="#888").grid(row=2, column=2, sticky="w")

        ttk.Label(lf, text="代理:").grid(row=3, column=0, sticky="e")
        self.proxy_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.proxy_var, width=18).grid(row=3, column=1, sticky="w")
        ttk.Label(lf, text="（host:port，如 127.0.0.1:7897；留空=直连）", foreground="#888").grid(row=3, column=2, sticky="w")

        ttk.Label(lf, foreground="#666", wraplength=740, justify="left", text=(
            "首次使用：浏览器打开 https://my.telegram.org → 用手机号登录 → API development tools "
            "→ 随便填个应用标题创建 → 把 api_id 和 api_hash 复制到上面（只需一次，会自动保存）。"
            "若已用 convert_tdata.py 转换过 AyuGram 登录，此处无需改动。"
        )).grid(row=4, column=0, columnspan=3, sticky="we", pady=(4, 0))

        lrow = ttk.Frame(lf)
        lrow.grid(row=5, column=0, columnspan=3, sticky="we", pady=6)
        self.login_btn = ttk.Button(lrow, text="保存并登录", command=self.on_login)
        self.login_btn.pack(side="left")
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

        # ---------- ② 下载区 ----------
        df = ttk.LabelFrame(main, text="② 下载 PDF", padding=8)
        df.pack(fill="x", pady=8)
        df.columnconfigure(1, weight=1)

        ttk.Label(df, text="群/频道:").grid(row=0, column=0, sticky="e")
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(df, textvariable=self.chat_var)
        self.chat_entry.grid(row=0, column=1, sticky="we")
        ttk.Label(df, text="@用户名 / t.me/链接 / 数字ID", foreground="#888").grid(row=0, column=2, sticky="w")

        ttk.Label(df, text="保存到:").grid(row=1, column=0, sticky="e")
        self.out_var = tk.StringVar(value=core.DEFAULT_OUT)
        ttk.Entry(df, textvariable=self.out_var).grid(row=1, column=1, sticky="we")
        ttk.Button(df, text="浏览…", command=self.on_browse).grid(row=1, column=2, sticky="w", padx=(4, 0))

        ttk.Label(df, text="扫描条数:").grid(row=2, column=0, sticky="e")
        self.limit_var = tk.IntVar(value=0)
        ttk.Spinbox(df, from_=0, to=10 ** 9, textvariable=self.limit_var, width=10).grid(row=2, column=1, sticky="w")
        ttk.Label(df, text="0 = 扫描全部历史消息", foreground="#888").grid(row=2, column=2, sticky="w")

        brow = ttk.Frame(df)
        brow.grid(row=3, column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.start_btn = ttk.Button(brow, text="开始下载 PDF", command=self.on_start, state="disabled")
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(brow, text="取消", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(brow, text="打开下载目录", command=self.on_open_dir).pack(side="left")

        self.progress = ttk.Progressbar(df, maximum=100)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 2))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(df, textvariable=self.status_var, foreground="#333").grid(row=5, column=0, columnspan=3, sticky="w")

        # ---------- 日志 ----------
        lf2 = ttk.LabelFrame(main, text="日志", padding=4)
        lf2.pack(fill="both", expand=True)
        self.log_text = tk.Text(lf2, height=12, state="disabled", wrap="word", font=("Consolas", 9))
        ys = ttk.Scrollbar(lf2, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ys.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)
        self._append_log("提示：登录一次后，以后双击 download_pdfs.bat → 填群/频道链接 → 点【开始下载 PDF】即可。")
        if self.api_id_var.get() and self.api_hash_var.get():
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
        cfg["api_id"] = self.api_id_var.get().strip()
        cfg["api_hash"] = self.api_hash_var.get().strip()
        cfg["phone"] = self.phone_var.get().strip()
        cfg["out_dir"] = self.out_var.get().strip()
        cfg["chat"] = self.chat_var.get().strip()
        cfg["limit"] = self.limit_var.get()
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

    def _start_login(self, interactive):
        if self.mode:
            return
        try:
            api_id = int(self.api_id_var.get().strip())
        except ValueError:
            messagebox.showerror("提示", "API ID 必须是数字（在 my.telegram.org 获取）。")
            return
        api_hash = self.api_hash_var.get().strip()
        phone = self.phone_var.get().strip()
        if not api_hash or not phone:
            messagebox.showerror("提示", "请填写 API HASH 和手机号。")
            return
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

    # ---------- 下载 ----------
    def on_start(self):
        if self.mode or not self.authed:
            return
        chat = self.chat_var.get().strip()
        out = self.out_var.get().strip()
        if not chat:
            messagebox.showerror("提示", "请填写群/频道链接，例如 @durov 或 t.me/durov。")
            return
        if not out:
            messagebox.showerror("提示", "请选择保存目录。")
            return
        try:
            api_id = int(self.api_id_var.get().strip())
            limit = int(self.limit_var.get())
        except ValueError:
            messagebox.showerror("提示", "API ID / 扫描条数必须是数字。")
            return
        if not self._save_config():
            return
        self._set_busy("download")
        self.worker = DownloadWorker(api_id, self.api_hash_var.get().strip(), chat, out, limit, self.q)
        self.worker.start()

    def on_cancel(self):
        if self.mode == "download" and isinstance(self.worker, DownloadWorker):
            self.worker.cancel = True
            self._append_log("正在取消…（当前文件下载完或扫描到当前位置后停止）")

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
        self.login_btn.config(state="disabled")
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="enabled" if mode == "download" else "disabled")
        if mode == "login":
            self.status_var.set("正在登录/检查会话…")
        else:
            self.status_var.set("准备中…")

    def _set_idle(self):
        self.mode = None
        self.cancel_btn.config(state="disabled")
        self.login_btn.config(state="enabled")
        self.start_btn.config(state="enabled" if self.authed else "disabled")

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
        elif kind == "found":
            self.progress.config(value=0)
            self.status_var.set(f"找到 {item[1]} 个 PDF，开始下载…")
        elif kind == "dl":
            i, n, name, cur, tot = item[1:]
            pct = ((i - 1) + (cur / tot if tot else 1)) / n * 100
            self.progress.config(value=min(pct, 100))
            self.status_var.set(f"下载 {i}/{n}：{name}（{core.human_size(cur)} / {core.human_size(tot)}）")
        elif kind == "done":
            ok, skip, fail = item[1:]
            self._append_log(f"全部完成：成功 {ok}，跳过 {skip}，失败 {fail}")
            self.status_var.set(f"完成：成功 {ok}，跳过 {skip}，失败 {fail}")
            if ok:
                self.progress.config(value=100)
            self._set_idle()
        elif kind == "error":
            self._append_log(item[1])
            messagebox.showerror("出错了", item[1])
            self._set_idle()
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
