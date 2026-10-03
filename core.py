# -*- coding: utf-8 -*-
"""Telethon 核心：登录流程、扫描并批量下载 PDF 的逻辑（GUI 与 CLI 共用）。

下载阶段为多连接并行：用同一授权密钥克隆 N 个内存会话客户端，
每条连接同时下载不同文件，绕开 Telegram 免费账号的单连接限速。
"""
import asyncio
import json
import os
import re
import sys
import time

from telethon import TelegramClient, errors

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(APP_DIR, "session_downloader")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "downloader.log")

PDF_MIME = "application/pdf"
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads")


def log_file(text):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {text.rstrip()}\n")
    except OSError:
        pass


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "unnamed.pdf"


def is_pdf(file):
    name = file.name or ""
    return file.mime_type == PDF_MIME or name.lower().endswith(".pdf")


def display_name(me):
    name = " ".join(x for x in [me.first_name, me.last_name] if x)
    return name or ("@" + me.username if me.username else str(me.id))


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def detect_proxy():
    """探测 Windows 系统代理（AyuGram 用的也是它），返回 dict 或 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not enabled or not server:
            return None
        socks = plain = None
        for part in server.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip().lower() == "socks":
                    socks = v.strip()
            else:
                plain = part.strip()
        host_port = socks or plain
        if not host_port:
            return None
        host, _, port = host_port.rpartition(":")
        return {"type": "socks5", "host": host or "127.0.0.1", "port": int(port)}
    except (OSError, ValueError):
        return None


def parse_proxy_text(text):
    """解析用户输入的代理：host:port / socks5://host:port / http://host:port。"""
    text = (text or "").strip()
    if not text:
        return None
    ptype = "socks5"
    low = text.lower()
    for prefix, t in (("socks5://", "socks5"), ("socks://", "socks5"), ("http://", "http")):
        if low.startswith(prefix):
            ptype, text = t, text[len(prefix):]
            break
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"代理格式不对：{text}（应为 host:port，如 127.0.0.1:7897）")
    return {"type": ptype, "host": host, "port": int(port)}


def connection_hint(e):
    """连接类错误给出一眼能懂的提示。"""
    if isinstance(e, ConnectionError) or "onnect" in str(e) or "Timeout" in type(e).__name__:
        return ("\n可能是网络不通或代理失效：请检查界面里的“代理”框"
                "（如 127.0.0.1:7897）、代理软件是否在运行。")
    return ""


def make_client(session=SESSION_PATH, api_id=None, api_hash=None):
    """创建带代理配置的 TelegramClient（代理写在 config.json 的 proxy 字段）。

    session 可以是会话文件路径，也可以是现成的 Session 对象（并行工作连接用）。
    """
    cfg = load_config()
    kwargs = {}
    p = cfg.get("proxy") or {}
    if p.get("host") and p.get("port"):
        kwargs["proxy"] = {"proxy_type": p.get("type", "socks5"),
                           "addr": p["host"], "port": int(p["port"])}
    return TelegramClient(session,
                          int(api_id or cfg.get("api_id") or 0),
                          api_hash or cfg.get("api_hash") or "",
                          **kwargs)


async def login_flow(client, phone, *, log, ask_code, ask_password):
    """交互式登录：请求验证码 → 登录 →（如开启两步验证）输入云密码。"""
    log("正在向 Telegram 请求验证码…")
    await client.send_code_request(phone)
    log("验证码已发送（通常会直接作为 Telegram 消息发给你，注意查看 AyuGram/其他设备）。")
    code = ask_code()
    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        log("该账号开启了两步验证，请输入云密码。")
        await client.sign_in(password=ask_password())
    return await client.get_me()


def parse_chat_input(s):
    """把用户输入解析成 Telethon 能用的 chat 标识。

    支持：@用户名、t.me/用户名、t.me/c/数字（私有频道）、数字 ID（-100…）。
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("请填写群/频道链接或 ID")
    m = re.match(r"(?:https?://)?t\.me/c/(\d+)", s, re.I)
    if m:
        return int("-100" + m.group(1))
    m = re.match(r"(?:https?://)?t\.me/(.+)$", s, re.I)
    if m:
        rest = m.group(1)
        if rest.startswith(("joinchat/", "+")):
            raise ValueError(
                "这是邀请链接（t.me/+xxx），工具无法直接定位。"
                "请先用 AyuGram 加入该群，再填 @用户名 或数字 ID。"
            )
        name = rest.lstrip("@").split("/")[0].strip()
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", name):
            return "@" + name
        raise ValueError("无法识别的 t.me 链接。")
    if s.startswith("@"):
        return s
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"[A-Za-z0-9_]{4,64}", s):
        return "@" + s
    raise ValueError("无法识别的目标。支持：@用户名、t.me/用户名、t.me/c/数字、数字 ID")


async def run_download(api_id, api_hash, chat_input, out_dir, limit, *,
                       session_path=SESSION_PATH, log, on_scan, on_found,
                       on_progress, cancelled, workers=4):
    """扫描目标群/频道的历史消息并并行下载所有 PDF。

    返回 (成功数, 跳过数, 失败数)。失败/找不到目标时抛 RuntimeError。
    """
    chat = parse_chat_input(chat_input)
    os.makedirs(out_dir, exist_ok=True)
    workers = max(1, min(int(workers or 1), 8))

    async with make_client(session_path, api_id, api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("尚未登录，请先登录。")

        log(f"正在定位 {chat_input} …")
        try:
            entity = await client.get_entity(chat)
        except Exception:
            raise RuntimeError(
                "找不到该群/频道。公开频道用 @用户名 或 t.me/用户名；"
                "私有频道请先用 AyuGram 打开过一次，或直接填数字 ID（如 -1001234567890）。"
            )
        title = (getattr(entity, "title", None)
                 or ("@" + (entity.username or "")) or str(chat))
        log(f"目标：{title}，开始扫描历史消息（{'全部' if not limit else limit} 条内）…")

        found = []
        scanned = 0
        async for msg in client.iter_messages(entity, limit=limit or None):
            if cancelled():
                log("已取消扫描。")
                return 0, 0, 0
            scanned += 1
            f = msg.file
            if f and is_pdf(f):
                found.append((msg, f.name or f"pdf_{msg.id}.pdf", f.size or 0))
            if scanned % 20 == 0:
                on_scan(scanned)
        on_scan(scanned)

        total_files = len(found)
        total_bytes = sum(s for _, _, s in found)
        log(f"扫描完成：{scanned} 条消息，找到 {total_files} 个 PDF，共 {human_size(total_bytes)}。")
        if not found:
            return 0, 0, 0
        on_found(total_files, total_bytes)

        # 为每条工作连接克隆一份已授权的会话文件（同一账号密钥、各自独立
        # 的 TCP 连接），绕开 Telegram 免费账号的单连接限速。
        import shutil
        main_session_file = session_path + ".session"
        queue = asyncio.Queue()
        for i, (msg, raw_name, size) in enumerate(found, 1):
            queue.put_nowait((i, msg, raw_name, size))

        stat = {"ok": 0, "skip": 0, "fail": 0, "done_bytes": 0}
        live = {}  # 工作连接 idx -> 当前文件已下载字节

        def report(name):
            on_progress(stat["ok"] + stat["skip"] + stat["fail"], total_files,
                        stat["done_bytes"] + sum(live.values()), total_bytes, name)

        async def download_one(wclient, widx, i, msg, raw_name, size):
            fname = sanitize_filename(raw_name)
            if not fname.lower().endswith(".pdf"):
                fname += ".pdf"
            final = os.path.join(out_dir, fname)
            part = final + ".part"
            if os.path.exists(final):
                esize = os.path.getsize(final)
                if esize == size:
                    stat["skip"] += 1
                    stat["done_bytes"] += size
                    log(f"[{i}/{total_files}] 已存在，跳过：{fname}")
                    report(fname)
                    return
                if esize < size:
                    log(f"[{i}/{total_files}] 发现不完整的旧文件，重新下载：{fname}")
                else:  # 同名但内容不同，避免覆盖
                    stem, ext = os.path.splitext(fname)
                    final = os.path.join(out_dir, f"{stem}_{msg.id}{ext}")
                    part = final + ".part"
            if os.path.exists(part):
                os.remove(part)

            last = [0]

            def cb(cur, tot):
                live[widx] = cur
                if tot and (cur - last[0] >= max(tot // 50, 262144) or cur >= tot):
                    last[0] = cur
                    report(fname)

            done = False
            for attempt in (1, 2):
                if cancelled():
                    return
                try:
                    await wclient.download_media(msg, file=part, progress_callback=cb)
                    os.replace(part, final)
                    done = True
                    stat["ok"] += 1
                    stat["done_bytes"] += os.path.getsize(final)
                    log(f"[{i}/{total_files}] 已下载：{fname}（{human_size(size)}）")
                    report(fname)
                    return
                except errors.FloodWaitError as e:
                    wait = int(getattr(e, "seconds", None) or getattr(e, "value", 0) or 30)
                    log(f"[{i}/{total_files}] 触发限流，等待 {wait} 秒后重试：{fname}")
                    await asyncio.sleep(min(wait, 120) + 1)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"[{i}/{total_files}] 下载失败：{fname}（{e}）")
                    break
            if not done and os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if not done:
                stat["fail"] += 1
                report(fname)

        async def worker_loop(widx):
            wfile = f"{session_path}_w{widx}.session"
            try:
                if not os.path.exists(wfile):
                    shutil.copyfile(main_session_file, wfile)
            except OSError as e:
                log(f"工作连接 {widx + 1} 会话克隆失败：{e}")
                return
            async with make_client(wfile, api_id, api_hash) as wclient:
                if not await wclient.is_user_authorized():
                    log(f"工作连接 {widx + 1} 授权失败，该连接退出。")
                    return
                while not cancelled():
                    try:
                        i, msg, raw_name, size = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    live[widx] = 0
                    try:
                        await download_one(wclient, widx, i, msg, raw_name, size)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # 单个任务的意外错误不允许拖死整条工作连接
                        core_log = f"[{i}/{total_files}] 任务异常，跳过：{e}"
                        log(core_log)
                        stat["fail"] += 1
                        report(raw_name)
                    finally:
                        live.pop(widx, None)

        async def cancel_monitor(tasks):
            while not cancelled():
                await asyncio.sleep(0.5)
            for t in tasks:
                t.cancel()

        log(f"启动 {workers} 条并行连接开始下载…")
        tasks = [asyncio.create_task(worker_loop(k)) for k in range(workers)]
        mon = asyncio.create_task(cancel_monitor(tasks))
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            mon.cancel()
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                log(f"工作连接异常退出：{r}")

        log(f"下载结束：成功 {stat['ok']}，跳过 {stat['skip']}，失败 {stat['fail']}。")
        return stat["ok"], stat["skip"], stat["fail"]
