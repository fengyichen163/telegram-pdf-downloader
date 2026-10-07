# -*- coding: utf-8 -*-
"""本地备份编排：扫描/过滤/排序（复用 mirror_core）→ 媒体逐个下载（复用下载机制）
→ 聊天式 HTML 渲染（export_html）→ 可选打包（单包 zip64 / 按体积分卷）。

三种导出形态：
  files    散件式：media/ 下按顺序命名的独立文件 + 聊天式 HTML 索引
  zip      打包式：整个备份目录压成单个 zip64 包（超大也支持）
  volumes  打包式（分卷）：按体积分成多个自包含 zip（每卷 ≤ --vol-gb，默认 3.8GB），
           每卷含自己的 index/pages/所需媒体，单独解压即可看

增量：媒体下载复用 download_entries 的"同大小跳过"，时间正序下重跑只补新消息；
改过滤/排序会改变文件名序号，等于重新导出（散件式按序号保序）。
"""
import asyncio
import os
import shutil
import time
import zipfile

from telethon import utils

import core
import export_html
import filters
import mirror_core

VOL_BYTES = 3800 * 1024 * 1024  # 分卷单卷上限（留出 zip 结构余量，<4GB）


def _ordered_name(idx, entry, msg):
    """按导出顺序命名的媒体文件名：00001_原名.ext —— 资源管理器里顺序即聊天顺序。"""
    f = msg.file
    if msg.photo:
        base = f"photo_{entry['id']}.jpg"
    elif f and f.name:
        base = f.name
    else:
        base = f"file_{entry['id']}.{entry['ext'] or 'bin'}"
    return f"{idx:05d}_" + core.sanitize_filename(base)


async def plan_backup(client, src_input, rules, order, limit, *, log, cancelled):
    """扫描 + 过滤 + 排序，返回 (entries, stats, src_entity, title)。

    entries 形态 = 下载器条目 + 渲染字段（mfile/own/entities/msg），
    可直接喂给 core.download_entries。
    """
    src_entity = await mirror_core._resolve(client, src_input)
    rules = await mirror_core._resolve_senders(client, rules or {}, log)
    msgs = {}
    plan, scanned = await mirror_core._build_plan(
        client, src_entity, limit, log, cancelled, msgs_out=msgs)
    crules = filters.compile_rules(rules)
    kept = [p for p in plan if filters.matches(p, crules)]
    me = await client.get_me()
    own_id = utils.get_peer_id(me)
    entries = []
    for i, p in enumerate(mirror_core._order_plan(kept, order), 1):
        m = msgs.get(p["id"])
        entry = dict(p)
        entry["own"] = (p["sender_id"] == own_id)
        entry["entities"] = list(m.entities or []) if m else None
        entry["msg"] = m
        if m and p["has_media"]:
            entry["mfile"] = _ordered_name(i, p, m)
            entry["mime"] = (m.file.mime_type if m.file else "") or ""
        else:
            entry["mfile"] = None
        entries.append(entry)
    title = getattr(src_entity, "title", None) or src_input
    stats = {"scanned": scanned, "filtered_out": len(plan) - len(kept),
             "media_cnt": sum(1 for e in entries if e["mfile"]),
             "media_bytes": sum(e["size"] for e in entries if e["mfile"])}
    return entries, stats, src_entity, title


def _zip_into(zf, base_dir, rel_files, log=None):
    """把 base_dir 下相对路径列表写入 zf（媒体 STORED、HTML DEFLATED）。"""
    n = 0
    for rel in rel_files:
        src = os.path.join(base_dir, rel)
        if not os.path.isfile(src):
            continue
        comp = zipfile.ZIP_DEFLATED if rel.endswith((".html", ".css")) \
            else zipfile.ZIP_STORED
        zf.write(src, rel, compress_type=comp)
        n += 1
    return n


def _split_volumes(entries, page_size, vol_bytes=VOL_BYTES):
    """按每页媒体字节量贪心分卷，返回 [卷内页码列表]，相册/页不跨卷。"""
    page_bytes = export_html.page_media_bytes(entries, page_size)
    vols, cur, cur_bytes = [], [], 0
    for pi, pb in enumerate(page_bytes, 1):
        if cur and cur_bytes + pb > vol_bytes:
            vols.append(cur)
            cur, cur_bytes = [], 0
        cur.append(pi)
        cur_bytes += pb
    if cur:
        vols.append(cur)
    return vols


def _entries_of_pages(entries, pages, page_size=None):
    page_size = page_size or export_html.PAGE_SIZE
    out = []
    for p in pages:
        out.extend(entries[(p - 1) * page_size:p * page_size])
    return out


async def export_backup(api_id, api_hash, src_input, out_dir, mode, opts, *,
                        log, on_progress, cancelled):
    """mode: files | zip | volumes；opts: rules/order/limit/workers/page_size/vol_gb。"""
    order = opts.get("order") or "time_asc"
    page_size = int(opts.get("page_size") or export_html.PAGE_SIZE)

    stats = {"mode": mode, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "zip_files": []}
    async with core.connected_client(api_id=api_id, api_hash=api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("尚未登录，请先登录。")
        log("扫描源消息（含纯文本）…")
        entries, pstats, src_entity, title = await plan_backup(
            client, src_input, opts.get("rules"), order,
            int(opts.get("limit") or 0), log=log, cancelled=cancelled)
        stats.update(pstats)
        stats["title"] = title
        log(f"过滤排序后 {len(entries)} 条，其中媒体 {stats['media_cnt']} 个 / "
            f"{core.human_size(stats['media_bytes'])}。")

        if not entries:
            log("没有可导出的消息。")
            return stats

        staging = os.path.join(out_dir, core.sanitize_filename(
            f"{title}_{time.strftime('%Y%m%d')}"))
        media_dir = os.path.join(staging, "media")
        os.makedirs(media_dir, exist_ok=True)

        dl = [e for e in entries if e["mfile"]]
        if dl:
            log(f"逐个下载 {len(dl)} 个媒体到 media/（同大小自动跳过=增量）…")
            await core.download_entries(
                api_id, api_hash, dl, media_dir, chat_input=src_input,
                log=log,
                on_found=lambda n, tb: on_progress(0, 100, f"待下载 {n} 个媒体"),
                on_progress=lambda df, tf, bd, tb, name: on_progress(
                    (bd / tb * 100) if tb else 100, 100,
                    f"媒体 {df}/{tf} · {name}"),
                cancelled=cancelled, workers=int(opts.get("workers") or 3))
        if cancelled():
            log("已取消（已下载的媒体保留，重跑自动续）。")
            return stats

        log("渲染聊天式 HTML…")
        ok_cnt = sum(1 for e in dl
                     if os.path.isfile(os.path.join(media_dir, e["mfile"])))
        stats["media_ok"] = ok_cnt
        if ok_cnt < len(dl):
            log(f"注意：{len(dl) - ok_cnt} 个媒体未就绪（下载失败/被取消），"
                "HTML 中对应条目会显示为占位卡片，重跑可补。")
        pages = export_html.render(staging, title, entries, stats, page_size)

        if mode == "files":
            log(f"导出完成（散件式）：{staging}\n双击 index.html 查看。")
            stats["out"] = staging
        elif mode == "zip":
            zip_path = staging + ".zip"
            log(f"打包单个 zip64（媒体不再二次压缩，只压 HTML）…")
            with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
                rels = ["index.html"] + pages
                media_dir_rel = os.path.relpath(media_dir, staging).replace("\\", "/")
                for e in dl:
                    rels.append(f"{media_dir_rel}/{e['mfile']}")
                n = _zip_into(zf, staging, rels)
            stats["out"] = zip_path
            stats["zip_files"] = [zip_path]
            log(f"打包完成：{zip_path}（{n} 个文件）。")
        else:  # volumes
            vol_bytes = int(opts.get("vol_gb") or 0) * 1024 ** 3 or VOL_BYTES
            vols = _split_volumes(entries, page_size, vol_bytes)
            log(f"按体积分卷：{len(vols)} 卷（单卷上限 {core.human_size(vol_bytes)}）…")
            tmp = os.path.join(staging, "_vol")
            for vi, pages_in_vol in enumerate(vols, 1):
                if cancelled():
                    log("已取消分卷打包（前面卷已生成）。")
                    break
                sub_entries = _entries_of_pages(entries, pages_in_vol, page_size)
                if os.path.isdir(tmp):
                    shutil.rmtree(tmp)
                os.makedirs(tmp)
                vpages = export_html.render(tmp, title, sub_entries, stats, page_size)
                zip_path = staging + f".part{vi:02d}.zip"
                with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:
                    rels = ["index.html"] + vpages
                    for e in sub_entries:
                        if e["mfile"]:
                            rels.append(f"media/{e['mfile']}")
                    n = _zip_into(zf, tmp, rels)
                    n += _zip_into(zf, staging, [f"media/{e['mfile']}"
                                                 for e in sub_entries if e["mfile"]])
                stats["zip_files"].append(zip_path)
                log(f"卷 {vi}/{len(vols)}：{os.path.basename(zip_path)}（{n} 个文件）")
            if os.path.isdir(tmp):
                shutil.rmtree(tmp)
            stats["out"] = staging
            log("分卷打包完成：每卷自带 index.html，单独解压即可看。")
    return stats
