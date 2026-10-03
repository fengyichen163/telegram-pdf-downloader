# Telegram 群/频道 PDF 一键下载器

批量下载指定 Telegram 群组 / 频道里**所有 PDF 文件**的本地工具。

> 为什么不是 AyuGram 插件？AyuGram 是 Telegram Desktop 的分支，Telegram 桌面端
> **没有插件系统**，无法给它装第三方插件。本工具用你自己的账号通过官方 MTProto
> 接口登录，与 AyuGram 客户端并存使用，效果等同"一键下载群/频道所有 PDF"。

## 功能

- 📁 扫描群/频道**全部历史消息**，自动识别 PDF（按 MIME 类型 + 文件名后缀）
- ⬇️ 逐个下载，实时进度条 + 日志，可随时取消
- 🔁 **断点续传**：已下载过（文件名和大小都相同）的自动跳过，重跑即续传
- 🔒 会话保存在本目录，登录一次后以后双击即用
- 🖥️ 图形界面（GUI）和命令行（CLI）两个版本

## 登录（两种方式任选）

### 方式 A：复用 AyuGram 的登录（推荐，已配置则跳过）

如果你的 AyuGram 已经登录，直接把它的凭证转换过来，**无需申请 api_id、无需验证码**：

```bat
python convert_tdata.py
```

- 自动读取旁边的 `..\tdata`，解密出当前活跃账号的授权密钥，生成 `session_downloader.session`
- 自动探测 Windows 系统代理（AyuGram 用的也是它）并写入配置
- 只读取 tdata，不影响 AyuGram；转换成功后 Telegram 里可能出现一条“新登录”提示，属正常现象
- 多账号时用 `python convert_tdata.py 1` 转换指定账号（启动时会列出所有账号及索引）
- tdata 设过本地密码时：`python convert_tdata.py --passcode 你的密码`

### 方式 B：验证码登录

1. 浏览器打开 **https://my.telegram.org** ，用你的 Telegram 手机号登录（验证码会发到你的 Telegram）。
2. 点 **API development tools** → 随便填一个 App title（如 `mydownloader`）→ 创建。
3. 页面上会显示 **api_id**（一串数字）和 **api_hash**（一串字母数字）。
4. 双击 `download_pdfs.bat` 启动图形界面，把 api_id、api_hash、手机号（如 `+8613800138000`）填进去 → 点【保存并登录】。
5. 输入收到的验证码（若开启了两步验证，再输入云密码）→ 登录成功。

### 代理

- 登录和下载都会自动使用 `config.json` 里 `proxy` 字段的代理（转换 tdata 时自动探测系统代理写入）。
- 需要手动指定/修改时，编辑 `config.json`：
  ```json
  "proxy": { "type": "socks5", "host": "127.0.0.1", "port": 7897 }
  ```
  支持 `socks5` 和 `http`；留空 `"proxy": null` 则直连。

## 日常使用

1. 双击 `download_pdfs.bat`
2. 在【群/频道】里填：`@频道用户名`、`t.me/xxxx` 链接或数字 ID
   - 私有频道：先用 AyuGram 打开过该频道，然后填 `t.me/c/1234567/8` 这类链接或数字 ID（`-100…`）
3. 选好保存目录 → 点【开始下载 PDF】，等进度条走完即可

命令行版（功能相同，方便脚本化）：

```bat
python pdf_downloader_cli.py @channel_name
python pdf_downloader_cli.py t.me/xxxx -o D:\pdfs -n 2000
```

`-n` 限制最多扫描多少条消息（默认 0 = 全部历史）。

## 常见问题

| 问题 | 说明 |
| --- | --- |
| 找不到频道 | 私有频道需先用 AyuGram 打开过（让账号有该会话缓存），或直接用数字 ID |
| 提示限流 FloodWait | Telegram 对请求频率有限制，工具会自动等待并跳过；稍后重新运行即可续传 |
| 下载很慢 | 可选安装加速库：`pip install cryptg`（启动脚本会自动尝试） |
| 换电脑/换号 | 删除本目录的 `session_downloader.session` 重新登录即可 |

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `download_pdfs.bat` | 一键启动图形界面（自动装依赖） |
| `pdf_downloader_gui.py` | 图形界面版 |
| `pdf_downloader_cli.py` | 命令行版 |
| `convert_tdata.py` | 复用 AyuGram 登录（tdata → 会话，免验证码） |
| `inspect_tdata.py` | tdata 结构诊断工具（一般用不到） |
| `core.py` | 核心逻辑（登录、扫描、下载、续传） |
| `config.json` | 自动生成的配置（api_id / api_hash / 代理等） |
| `session_downloader.session` | 登录会话，**等同账号凭证，请勿外传** |
| `downloader.log` | 运行日志 |

## 注意事项

- 工具只能下载**你的账号有权限看到**的内容（你已加入的群/频道），仅建议用于个人备份/学习。
- 请遵守 Telegram 服务条款与文件版权；请勿用于大规模抓取或二次分发受版权保护的内容。
- `session_downloader.session` 与 `config.json` 含敏感信息，不要发给别人或上传网盘。

## 协议与致谢

- 基于 [Telethon](https://github.com/LonamiWebs/Telethon) 与 [opentele](https://github.com/thedemons/opentele) 实现。
- 采用 [MIT License](LICENSE) 开源。
