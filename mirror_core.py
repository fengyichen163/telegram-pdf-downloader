# -*- coding: utf-8 -*-
"""频道/群复制引擎：服务端批量转发为主，"禁止转发"频道回退为下载→重传。

- 不落地方案：媒体在服务端引用原文件（forward_messages），零下载流量、无 2GB 限制；
- drop_author=True 去掉"转发自"头，消息在目标里由你的账号发出（即"复制"）；
- noforwards 是服务端标记，API 层无解（AyuGram 的"自由转发"同样是下载重传模拟的），
  回退方案：媒体 ≤2GB 下载→重传，纯文本重发，超限/不支持类型计数跳过；
- 断点续传：copy_progress/copy_{src}_{dest}.json 记已完成的源消息 id，重跑=增量同步；
- 去重：目标频道已有同文件（file.unique_id）跳过 + 全局去重库（copy_dedup.sqlite3），
  文本消息不去重（同文重复多为正常聊天，去重会误删）；
- 重建回复引用（可选）：逐条发送并挂 reply_to（旧 id→新 id 映射），API 调用量大
  一个量级，限流风险高，默认走批量转发。
"""
import asyncio
import json
import os
import sqlite3
import time

from telethon import errors, functions, types, utils

import core
import filters

PROGRESS_DIR = os.path.join(core.APP_DIR, "copy_progress")
DEDUP_DB = os.path.join(core.APP_DIR, "copy_dedup.sqlite3")
FORWARD_BATCH = 100
TWO_GB = 2 * 1024 ** 3
ORDERS = ("time_asc", "time_desc", "size_asc", "size_desc")


# ---------- 进度 ----------

def _progress_path(src_id, dest_id):
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    return os.path.join(PROGRESS_DIR, f"copy_{src_id}_{dest_id}.json")


def load_progress(src_id, dest_id):
    try:
        with open(_progress_path(src_id, dest_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"done": [], "dest_map": {}}


def save_progress(path, prog):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prog, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------- 全局去重库 ----------

def _dedup_load():
    try:
        con = sqlite3.connect(DEDUP_DB)
        try:
            rows = con.execute("SELECT unique_id FROM copied_media").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def _dedup_add(uids, dest_desc):
    if not uids:
        return
    try:
        con = sqlite3.connect(DEDUP_DB)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS copied_media("
                "unique_id TEXT PRIMARY KEY, dest_chat TEXT, copied_at TEXT)")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            con.executemany(
                "INSERT OR IGNORE INTO copied_media VALUES (?,?,?)",
                [(u, dest_desc, now) for u in uids])
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


# ---------- 扫描 / 计划 ----------

def _media_uid(msg):
    """媒体的稳定去重键：同一文件被转发/重发后 Document/Photo id 不变。"""
    doc = getattr(msg, "document", None)
    if isinstance(doc, types.Document):
        return f"doc:{doc.id}"
    photo = getattr(msg, "photo", None)
    if isinstance(photo, types.Photo):
        return f"photo:{photo.id}"
    return None


def _sender_meta(msg):
    """(sender_id, 显示名, 用户名, 是否无署名)。无署名=匿名管理员/实体未知。"""
    if msg.post_author:
        return msg.sender_id, msg.post_author, "", False
    sid = msg.sender_id
    if sid is None:
        return None, "", "", False          # 频道帖，署名=频道本身
    s = msg.sender
    if s is None:
        return sid, "", "", True            # 实体未知
    if sid == msg.chat_id:
        return sid, "", "", True            # 以群身份发言（匿名管理员）
    name = " ".join(x for x in (getattr(s, "first_name", None),
                                getattr(s, "last_name", None)) if x)
    uname = getattr(s, "username", "") or ""
    if not name:
        name = ("@" + uname) if uname else (getattr(s, "title", "") or "")
    return sid, name, uname, False


async def _build_plan(client, src_entity, limit, log, cancelled, msgs_out=None):
    """遍历源消息产出轻量计划条目（不含 Message 对象，万级消息内存占用很小）。

    msgs_out 传 dict 时会顺带保存 id→Message 映射（本地备份需要原始对象
    下载媒体/取格式实体；复制功能用不到就不存，省内存）。
    """
    plan = []
    scanned = 0
    async for msg in client.iter_messages(src_entity, limit=limit or None):
        if cancelled():
            raise RuntimeError("已取消")
        scanned += 1
        if scanned % 500 == 0:
            log(f"已扫描 {scanned} 条消息…")
        if msg.action:
            continue                         # 进群/退群等服务消息不复制
        f = msg.file
        has_media = f is not None
        size = (f.size or 0) if f else 0
        uid = _media_uid(msg)
        nm = (f.name or "") if f else ""
        ext = nm.rsplit(".", 1)[1].lower() if "." in nm else ""
        sid, name, uname, anon = _sender_meta(msg)
        text = msg.message or ""
        if getattr(msg, "poll", None):
            text = f"[投票] {msg.poll.question}"
        if msgs_out is not None:
            msgs_out[msg.id] = msg
        plan.append({
            "id": msg.id, "gid": msg.grouped_id,
            "date": msg.date.date().isoformat() if msg.date else "",
            "time": msg.date.strftime("%H:%M") if msg.date else "",
            "size": size, "uid": uid, "ext": ext, "has_media": has_media,
            "text": text,
            "reply_id": msg.reply_to_msg_id,
            "sender_id": sid, "sender_name": name,
            "sender_username": uname, "anonymous": anon,
        })
    return plan, scanned


def _order_plan(plan, order):
    if order == "time_asc":
        plan.sort(key=lambda p: p["id"])
    elif order == "time_desc":
        plan.sort(key=lambda p: -p["id"])
    elif order == "size_asc":
        plan.sort(key=lambda p: (p["size"], p["id"]))
    elif order == "size_desc":
        plan.sort(key=lambda p: (-p["size"], p["id"]))
    return plan


def _batches(plan, size=FORWARD_BATCH):
    """切批；相册（grouped_id 相同）不拆开，否则转发后相册会散架。"""
    out, cur = [], []
    for p in plan:
        if cur and len(cur) >= size and (p["gid"] is None or p["gid"] != cur[-1]["gid"]):
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return out


async def _resolve(client, chat_input):
    try:
        return await client.get_entity(core.parse_chat_input(chat_input))
    except Exception:
        raise RuntimeError(
            f"找不到 {chat_input}。公开频道用 @用户名；私有频道先用客户端打开过一次，"
            "或填数字 ID。")


async def _resolve_senders(client, rules, log):
    """把 sender_allow/block 里的 @用户名解析成数字 id 一并加入（昵称/ID 保持原样）。"""
    out = dict(rules)
    for key in ("sender_allow", "sender_block"):
        resolved = []
        for x in rules.get(key) or []:
            x = (x or "").strip()
            if not x:
                continue
            if x.startswith("@"):
                try:
                    ent = await client.get_entity(x)
                    resolved.append(str(utils.get_peer_id(ent)))
                except Exception:
                    log(f"警告：无法解析 {x}，仅按昵称匹配。")
            resolved.append(x)
        out[key] = resolved
    return out


async def _dest_uids(client, dest_entity, log):
    seen, n = set(), 0
    async for m in client.iter_messages(dest_entity, limit=None):
        uid = _media_uid(m)
        if uid:
            seen.add(uid)
        n += 1
        if n % 1000 == 0:
            log(f"扫描目标已有内容：{n} 条…")
    return seen


async def _create_dest(client, kind, title, about, log):
    up = await client(functions.channels.CreateChannelRequest(
        title=title[:128], about=(about or "")[:255], megagroup=(kind == "group")))
    ch = up.chats[0]
    label = "群组" if kind == "group" else "频道"
    try:
        invite = await client(functions.channels.ExportChatInviteRequest(ch))
        log(f"已创建{label}「{ch.title}」，邀请链接：{invite.link}")
    except Exception:
        log(f"已创建{label}「{ch.title}」。")
    return ch


# ---------- 发送 ----------

async def _copy_one_fallback(client, src_entity, dest_entity, p, stats, log, tmp_dir):
    """单条回退：noforwards 或批次失败时，下载→重传（文本则重发）。"""
    try:
        m = await client.get_messages(src_entity, ids=p["id"])
        if not m:
            stats["failed"] += 1
            return
        if m.media and not isinstance(m.media, types.MessageMediaWebPage):
            if p["size"] and p["size"] > TWO_GB:
                stats["skipped_protected"] += 1
                log(f"#{p['id']} 超过 2GB，受保护内容无法复制，已跳过。")
                return
            path = os.path.join(tmp_dir, f"copy_{p['id']}.part")
            try:
                await client.download_media(m, file=path)
                await client.send_file(dest_entity, path, caption=m.message or None)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        else:
            await client.send_message(dest_entity, m.message or "",
                                      formatting_entities=m.entities or None)
        stats["copied"] += 1
        await asyncio.sleep(1)
    except errors.FloodWaitError as e:
        await asyncio.sleep(min(int(getattr(e, "seconds", 0) or 30), 120) + 1)
        stats["failed"] += 1
        log(f"#{p['id']} 触发限流，计入失败（重跑可续）。")
    except Exception as e:
        stats["failed"] += 1
        log(f"#{p['id']} 回退复制失败：{e}")


async def _copy_forward(client, src_entity, dest_entity, plan, prog, prog_path,
                        stats, drop_author, log, on_progress, cancelled):
    done = set(prog.get("done") or [])
    total = len(plan)
    batches = _batches(plan)
    tmp_dir = os.path.join(PROGRESS_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    def flush():
        prog["done"] = sorted(done)
        save_progress(prog_path, prog)

    for bi, batch in enumerate(batches, 1):
        if cancelled():
            log(f"已取消：完成 {stats['copied']}/{total}，进度已保存，重跑自动续传。")
            return
        ids = [p["id"] for p in batch]
        fallback = False
        for attempt in (1, 2, 3):
            try:
                await client.forward_messages(dest_entity, ids, src_entity,
                                              drop_author=drop_author)
                for p in batch:
                    done.add(p["id"])
                    stats["copied"] += 1
                    if p["uid"]:
                        _dedup_add([p["uid"]], stats.get("dest_desc", ""))
                flush()
                on_progress(min(stats["copied"], total), total,
                            f"第 {bi}/{len(batches)} 批")
                break
            except errors.FloodWaitError as e:
                wait = min(int(getattr(e, "seconds", 0) or 30), 120) + 1
                log(f"触发限流，等待 {wait} 秒后重试本批…")
                await asyncio.sleep(wait)
            except errors.ChatForwardsRestrictedError:
                log("目标源开启了“禁止转发”，逐条回退为下载→重传（慢）…")
                fallback = True
                break
            except Exception as e:
                if attempt == 3:
                    log(f"整批失败（{e}），逐条回退以隔离坏条目…")
                    fallback = True
                else:
                    await asyncio.sleep(2)
        if fallback:
            for p in batch:
                if cancelled():
                    log("已取消，进度已保存。")
                    return
                await _copy_one_fallback(client, src_entity, dest_entity, p,
                                         stats, log, tmp_dir)
                done.add(p["id"])
            flush()
            on_progress(min(stats["copied"], total), total,
                        f"第 {bi}/{len(batches)} 批（回退）")
        await asyncio.sleep(1)


async def _copy_rebuild(client, src_entity, dest_entity, plan, prog, prog_path,
                        stats, log, on_progress, cancelled):
    mapping = prog.setdefault("dest_map", {})
    done = set(prog.get("done") or [])
    total = len(plan)
    sent = 0

    def flush():
        prog["done"] = sorted(done)
        save_progress(prog_path, prog)

    for chunk in _batches(plan, 200):
        if cancelled():
            log(f"已取消：完成 {sent}/{total}，进度已保存，重跑自动续传。")
            return
        msgs = await client.get_messages(src_entity, ids=[p["id"] for p in chunk])
        by_id = {m.id: m for m in msgs if m}
        i = 0
        while i < len(chunk):
            if cancelled():
                log("已取消，进度已保存。")
                return
            p = chunk[i]
            m = by_id.get(p["id"])
            if m is None:
                stats["failed"] += 1
                i += 1
                continue
            reply_to = mapping.get(str(p["reply_id"])) if p.get("reply_id") else None
            try:
                if p["gid"] is not None:
                    grp = [c for c in chunk[i:] if c["gid"] == p["gid"]]
                    gmsgs = [by_id.get(g["id"]) for g in grp]
                    docs = [gm.document or gm.photo for gm in gmsgs
                            if gm and (gm.document or gm.photo)]
                    caps = [(gm.message or "") for gm in gmsgs
                            if gm and (gm.document or gm.photo)]
                    new = await client.send_file(dest_entity, docs, caption=caps,
                                                 reply_to=reply_to)
                    new_ids = [x.id for x in new] if isinstance(new, list) \
                        else [new.id] * len(grp)
                    for g, nid in zip(grp, new_ids):
                        mapping[str(g["id"])] = nid
                        done.add(g["id"])
                        stats["copied"] += 1
                    sent += len(grp)
                    i += len(grp)
                    await asyncio.sleep(1)
                    continue
                if m.document or m.photo:
                    new = await client.send_file(dest_entity, m.document or m.photo,
                                                 caption=m.message or None,
                                                 reply_to=reply_to)
                elif m.media and not isinstance(m.media, types.MessageMediaWebPage):
                    stats["skipped_protected"] += 1
                    log(f"#{p['id']} 类型不支持逐条重建（投票等），已跳过。")
                    done.add(p["id"])
                    i += 1
                    continue
                else:
                    new = await client.send_message(dest_entity, m.message or "",
                                                    formatting_entities=m.entities or None,
                                                    reply_to=reply_to)
                mapping[str(p["id"])] = new.id
                done.add(p["id"])
                stats["copied"] += 1
                sent += 1
            except errors.FloodWaitError as e:
                await asyncio.sleep(min(int(getattr(e, "seconds", 0) or 30), 120) + 1)
                stats["failed"] += 1
            except Exception as e:
                stats["failed"] += 1
                log(f"#{p['id']} 发送失败：{e}")
            i += 1
            await asyncio.sleep(0.5)
            if sent % 20 == 0:
                on_progress(sent, total, f"#{p['id']}")
                flush()
    flush()
    on_progress(min(sent, total), total, "完成")


# ---------- 主入口 ----------

async def copy_chat(api_id, api_hash, src_input, dest, opts, *,
                    log, on_progress, cancelled):
    """dest: ("new", "channel"|"group", 标题, 简介) 或 ("existing", chat_input)。

    opts: rules / order / drop_author / rebuild_replies / dedup_dest /
          dedup_global / limit / dry_run
    """
    order = opts.get("order") or "time_asc"
    drop_author = opts.get("drop_author", True)
    rebuild = opts.get("rebuild_replies", False)
    dedup_dest = opts.get("dedup_dest", True)
    dedup_global = opts.get("dedup_global", True)
    limit = int(opts.get("limit") or 0)
    dry = opts.get("dry_run", False)

    stats = {"copied": 0, "skipped_dedup": 0, "skipped_protected": 0,
             "failed": 0, "scanned": 0, "filtered_out": 0, "dest_desc": ""}

    async with core.connected_client(api_id=api_id, api_hash=api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("尚未登录，请先登录。")
        src_entity = await _resolve(client, src_input)
        src_id = utils.get_peer_id(src_entity)

        fresh = False
        dest_entity = None
        dest_id = 0
        if dest[0] == "new":
            _, kind, title, about = dest
            label = "群组" if kind == "group" else "频道"
            stats["dest_desc"] = f"新建{label}「{title}」"
            if not dry:
                dest_entity = await _create_dest(client, kind, title, about, log)
                dest_id = utils.get_peer_id(dest_entity)
                fresh = True
        else:
            dest_entity = await _resolve(client, dest[1])
            dest_id = utils.get_peer_id(dest_entity)
            stats["dest_desc"] = getattr(dest_entity, "title", None) or dest[1]

        rules = await _resolve_senders(client, opts.get("rules") or {}, log)
        log(f"开始扫描源消息（{'全部' if not limit else f'{limit} 条内'}）…")
        plan, scanned = await _build_plan(client, src_entity, limit, log, cancelled)
        stats["scanned"] = scanned
        crules = filters.compile_rules(rules)
        kept = [p for p in plan if filters.matches(p, crules)]
        stats["filtered_out"] = len(plan) - len(kept)
        log(f"共 {scanned} 条消息，过滤后剩 {len(kept)} 条（排除 {stats['filtered_out']} 条）。")

        prog = {"done": [], "dest_map": {}}
        prog_path = None
        if not dry and dest_id:
            prog_path = _progress_path(src_id, dest_id)
            prog = load_progress(src_id, dest_id)
        done = set(prog.get("done") or [])
        if done:
            log(f"断点续传：已有 {len(done)} 条复制记录，自动跳过。")
        kept = [p for p in kept if p["id"] not in done]

        dest_uids = set()
        if dedup_dest and dest_entity is not None and not fresh:
            log("扫描目标已有内容（同文件去重）…")
            dest_uids = await _dest_uids(client, dest_entity, log)
        global_uids = _dedup_load() if dedup_global else set()
        final = []
        for p in kept:
            if p["uid"] and ((dest_uids and p["uid"] in dest_uids)
                             or (global_uids and p["uid"] in global_uids)):
                stats["skipped_dedup"] += 1
                done.add(p["id"])
            else:
                final.append(p)
        plan_f = _order_plan(final, order)
        total = len(plan_f)
        log(f"待复制 {total} 条 / {core.human_size(sum(p['size'] for p in plan_f))}"
            f"（去重跳过 {stats['skipped_dedup']} 条），顺序 {order}。")

        if dry:
            log("试运行结束：未创建目标、未发送任何消息。")
            return stats
        if total == 0:
            log("没有需要复制的消息。")
            return stats
        if rebuild:
            log("逐条发送模式（重建回复引用），注意：比批量转发慢且限流风险更高…")
            await _copy_rebuild(client, src_entity, dest_entity, plan_f, prog,
                                prog_path, stats, log, on_progress, cancelled)
        else:
            await _copy_forward(client, src_entity, dest_entity, plan_f, prog,
                                prog_path, stats, drop_author, log,
                                on_progress, cancelled)
    return stats
