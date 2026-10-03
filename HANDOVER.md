# 交接文档 — Telegram 群/频道文件批量下载器

> 最后更新：2026-10-03。本文档面向下一个维护者/协作者，目标是 15 分钟内接手。

## 1. 一句话概述

用用户自己的 Telegram 账号（MTProto）扫描指定群/频道的历史消息，列出其中全部文件，
支持格式筛选、文件名搜索、勾选下载，多连接并行下载 + 断点续传。GUI（tkinter）与 CLI 双端。

## 2. 基本信息

- 本地目录：`D:\download\AyuGram (1)\pdf-downloader\`（与 AyuGram 客户端安装目录并列）
- 仓库：GitHub `fengyichen163/telegram-pdf-downloader` + Gitee `wangyb163/telegram-pdf-downloader`
  - origin 配了**双 pushurl**，一次 `git push` 同时推两边；fetch 只走 GitHub
  - GitHub 推送走本机代理：全局 git 配置 `http.https://github.com.proxy = socks5h://127.0.0.1:7897`
- 运行环境：Windows 10/11，Python 3.13（pip 依赖见 §7），需要能访问 Telegram 的代理
- 启动：双击 `download_pdfs.bat`（自动装依赖、pythonw 启动 GUI）；CLI 见 §8

## 3. 当前状态（2026-10-03）

### 已实现且实测验证
- 登录：tdata 转换（免验证码）与验证码登录双通道；会话持久化
- 扫描：全类型文件（文档/视频/音频/图片，跳过贴纸/语音/视频笔记），实测 13300 条消息 ~2 分钟
- 筛选/选择：格式下拉框（扫描后自动生成、带数量排序）、关键字搜索、点行勾选、全选/反选
- 下载：多连接并行（1~8 可调，默认 4）、字节级进度、取消、FloodWait 自动等待、
  文件引用过期自动刷新、`.part` 断点续传、同名不同大小区分（加 `_msgId` 后缀）
- 实战：某频道 692 个目标（5.4 GB）完成 674 个 PDF（5.24 GB），其中约 18 个为频道重复文件

### 已知限制
- 2 个文件报 `Telegram is having internal issues`（服务端 DC 存储故障），客户端无解，
  过段时间重跑会自动只补失败件
- 速度天花板：免费账号聚合限速 ~0.8 MB/s（4 连接已喂饱；更快需 Telegram Premium 或更优节点）
- 无自动化测试（有离线逻辑自检脚本见 git 历史；建议后续补 pytest）

## 4. 文件导览

| 文件 | 职责 |
| --- | --- |
| `core.py` | 全部核心逻辑：代理探测/解析、make_client、登录流、run_scan、filter_entries、download_entries、run_download(扫描+筛选+下载的 CLI 便捷封装) |
| `pdf_downloader_gui.py` | tkinter 界面。线程模型：Tk 主线程 + 后台 threading.Thread（内部 `asyncio.run`）+ `queue.Queue` 消息泵（`_poll` 每 100ms）|
| `pdf_downloader_cli.py` | 命令行版，参数：`chat, -o, -n, -w, --ext, --search, --list-only, --api-id/--api-hash/--phone` |
| `convert_tdata.py` | 把 Telegram Desktop/AyuGram 的 tdata 解密并转成 Telethon 会话（**自研解析，见 §6**）|
| `inspect_tdata.py` | tdata 结构诊断工具（打印结构、不输出密钥），排障用 |
| `download_pdfs.bat` | 一键启动 GUI（检测并安装依赖，pythonw 启动）|
| `config.json` | 运行配置（**不入库**）：api_id/api_hash/phone/out_dir/chat/limit/workers/proxy |
| `session_downloader.session` + `session_downloader_w0..3.session` | 主会话 + 并行工作连接克隆的会话（**不入库，等同账号凭证**）|

## 5. 核心设计要点

### 登录
- 首选 `convert_tdata.py`：解密 tdata → 提取授权密钥 → 写入 Telethon SQLiteSession → 联网验证 → 写 config.json
- 验证码登录：`core.login_flow`（send_code_request → sign_in → 两步验证分支），GUI 通过 Bridge
  （threading.Event + 值对象）跨线程取验证码/密码

### 扫描
- `run_scan`：`iter_messages` 全量遍历，`classify_file(msg)` 归类（看 `msg.file`；
  排除 sticker/voice/video_note；photo 生成 `photo_{id}.jpg`；无文件名的用 mime 推断扩展名）
- 条目 dict：`{id, msg, name, size, ext, date, mime}`——**msg 对象保留在内存里供下载用**
- `filter_entries(entries, exts, search)`：exts 传 `["all"]`/None=全部；search 大小写不敏感

### 下载（`download_entries`）
- 并行原理：把已授权的 `session_downloader.session` **复制**成 `_w0..N.session`，
  每个工作线程各开一个 TelegramClient（独立 TCP），绕开免费账号单连接限速
- 任务队列 `asyncio.Queue` + N 个 worker 协程；`cancel_monitor` 半秒轮询取消标记，
  触发后 `task.cancel()` 即时中断
- 断点续传规则：目标文件已存在且大小==预期 → 跳过；大小<预期 → 视为半成品覆盖重下；
  大小>预期 → 同名不同内容，改存 `名字_{msgId}.pdf`
- 下载写 `.part` 临时文件，成功后 `os.replace` 原子改名
- 失败处理：FloodWait → 睡到上限 120s 重试（共 2 次机会）；其他异常第一次先
  `get_messages(ids)` 刷新文件引用再重试，仍失败才计失败；worker 级 try 隔离保证
  单文件异常不拖死连接
- 回调协议（GUI/CLI 共用）：`log / on_scan / on_found(n, total_bytes) /
  on_progress(done_files, total_files, bytes_done, total_bytes, name) / cancelled()`

### GUI 消息泵
后台线程通过 `q.put(("事件", 数据))` 汇报，事件：`log/scan/scanned/found/dl/done/error/
need_code/need_password/authed/not_authed`。改回调签名时两端要同步。

## 6. 踩坑记录（重要！）

1. **新版 tdata 格式 opentele 读不了**：opentele 1.15.1 只认旧版 `map0`。自研解析：
   - `key_datas`（ReadFile 自动补 `s/1/0` 后缀！传名要传 `key_data`）→ salt + passcodeKey
     + localKey（CreateLocalKey：SHA512(salt+passcode+salt) 后 pbkdf2-sha512，空密码 1 迭代）
   - info 块（BE 的 QDataStream，前面有 4 字节 LE 长度前缀）：`count + [index...] + active`
   - 账号目录名 = `ToFilePart(ComputeDataNameKey("data"))`（index 0）或 `"data#N"`（N=index+1），
     即 `D877F783D5D3EF8C` / `A7FDF864FBC10B77` 这种哈希目录
   - MTP 授权数据在**根目录**同名文件 `<目录名>s`：blockId==75 → blob = userId(int32) +
     mainDcId(int32)（若 `userId<<32|dcId == ~0` 则改读 u64+int32）+ 两组密钥表
     （count + [dcId(int32) + 256B key]）
2. **并行工作连接不能用 MemorySession 复制密钥**：会报 `The provided authorization is invalid
   (ImportAuthorization)` 导致 worker 陆续死掉、任务假性提前结束。必须复制 `.session` 文件。
3. **api_id 必须用 17349**（官方 Telegram Desktop，AyuGram 同款）：跨 DC 文件下载要
   ExportAuthorization/ImportAuthorization，api_id 与授权不配对会失败。opentele 的
   TelegramDesktop preset（2040）home DC 正常、跨 DC 会挂。config.json 现值即 17349。
4. **Windows 中文编码坑**：
   - curl 命令行参数会被 GBK 化 → Gitee API 提交中文描述变乱码。改用 Python + UTF-8 文件
     + `urllib.parse.urlencode` 发请求
   - git-bash 里 `tasklist /fi "..."` 的 `/fi` 会被路径转换弄坏，用 `tasklist | grep` 代替
   - CLI 长时间打印中文文件名：main() 里 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
5. **config.json 必须合并写入**：GUI/CLI 保存时先读旧文件再 update，否则会弄丢 proxy 等字段
   （曾导致 GUI 丢代理后直连超时，报 "Connection to Telegram failed 5 time(s)"）
6. **opentele 的 OpenTeleException 继承 BaseException 而非 Exception**：`except Exception`
   接不住，要用 `except BaseException`
7. **代理**：全局代理来自系统注册表探测（`core.detect_proxy`，ProxyEnable+ProxyServer），
   写进 config.json 的 `proxy` 字段（type=socks5，Clash mixed 端口兼容）。Telethon 用
   `proxy={"proxy_type":..., "addr":..., "port":...}` 字典形式
8. **网络波动**：GitHub 推送偶发 Empty reply/连接失败，重试即可；已把 github.com 固定走代理
9. **GUI 里禁用 `async with TelegramClient(...)`（2026-10-03 修）**：Telethon 的 `__aenter__`
   会调 `start()`，会话未授权时它用内置 `input()` 讨手机号——pythonw 无控制台直接崩，
   报"登录失败：input(): lost sys.stdin"，GUI 自己的 Bridge 验证码流程根本执行不到。
   一律用 `core.connected_client`（显式 connect/disconnect，不走 start()）。CLI 与
   convert_tdata.py 一直是显式 connect()，所以只有 GUI 踩到；复现方法：把 SESSION_PATH
   指到不存在的新会话，在 sys.stdin=None 下跑 LoginWorker。

## 7. 环境与依赖

```
python 3.13  (C:\Users\wang0\AppData\Local\Programs\Python\Python313)
telethon==1.45.0  cryptg  opentele==1.15.1 (带 PyQt5)  python-socks[asyncio]
```
- Gitee 推送凭证存在 `~/.git-credentials`（私人令牌，wangyb163 生成，勾选 projects 权限）
- 系统代理 127.0.0.1:7897（Clash 系 mixed 端口），AyuGram/下载器/GitHub 推送都依赖它

## 8. 常用操作手册

```bat
:: 日常下载（GUI）
download_pdfs.bat  →  已登录✔  →  填群/频道  →  扫描文件列表
                   →  格式下拉/搜索筛选  →  勾选  →  下载勾选的文件（或下载全部筛选结果）

:: CLI 示例
python pdf_downloader_cli.py @chan                     :: 默认只下 pdf
python pdf_downloader_cli.py @chan --ext all           :: 全部类型
python pdf_downloader_cli.py @chan --ext epub --search 入门
python pdf_downloader_cli.py @chan --ext all --list-only   :: 只列不下载
python pdf_downloader_cli.py @chan -o D:\dir -n 5000 -w 6  :: 目录/扫描上限/并发

:: 补漏：直接重跑即可，已存在文件自动跳过，只补失败件
:: 重新登录：删 session*.session 后走验证码登录，或重跑 convert_tdata.py
:: 提交发布：git add -A && git commit -m "..." && git push   （自动双仓库）
```

## 9. 安全须知

- `config.json`（含手机号）、`session*.session`（等同账号凭证）、`downloader.log` 均在
  `.gitignore` 内，**绝不能入库**；提交前可用 `git check-ignore config.json` 自检
- Gitee 令牌在 `~/.git-credentials`，泄露时去 Gitee 令牌页删除重新生成
- 工具只能下载账号有权限访问的内容；遵守 Telegram ToS 与版权，仅供个人备份

## 10. 后续可选方向

- pytest：`parse_chat_input / filter_entries / sanitize_filename / parse_proxy_text` 都是纯函数，好测
- 扫描结果缓存（避免每次重扫 1.3 万条）；跨 DC 失败件按 DC 分组借道重下
- 打包 exe（pyinstaller）发给非技术用户；格式下拉支持多选
- 下载限速友好模式（避开高峰 FloodWait）；按频道建子目录/按日期归档
