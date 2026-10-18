# astrbot_jmdownloader

AstrBot 插件：在聊天中发送 `/jm <ID>`，抓取 JM 章节图片，合成 PDF，并通过 OneBot v11 协议端（LLBot / NapCat）作为 QQ 群文件或私聊文件发送。

插件默认整本发送，整本发送失败时按章节边界自动分卷；章节 PDF 可本地缓存，支持随机/固定打开密码。

## 功能

- **两种内容范围**：整本、指定单章，或按 `--head/--tail/--latest/--range` 选取若干章合并发送。
- **两种发送格式**：默认合并为 PDF；`--type zip|7z|tar|tar.gz|tar.bz2|tar.xz` 时把各章 PDF 打包成压缩包发送。未写 `--type` 时使用配置 `default_file_type`。
- **本子详情**：`/jm <ID> --info` 查询详情页信息（标题/作者/标签/页数/章节/日期/简介），同时下载并发送封面，详情优先以图片发送；不下载章节。
- **订阅更新**：`--sub loop <间隔>`/`--sub cron <表达式>` 定时检查本子更新，按配置直接推送文件或消息通知，支持群聊/私聊多目标；订阅可在插件 WebUI「订阅管理」页维护。
- **预览模式**：`--pre [N]` 取所选范围（整本/单章/一条范围）第一章的前 N 页，按 `--type` 或配置 `default_file_type` 发送 PDF/压缩包，密码按配置；N 省略或写 0 时使用配置 `preview_pages`（默认 10），可与 `--type`、章节筛选任意顺序混用。
- **整本优先、失败分卷**：先尝试单文件上传；只有整本上传失败才进入分卷回退，分卷切点只落在章节之间。
- **按实测体积规划分卷**：卷数、单卷目标与每卷章节数由整本 PDF 大小、各章 PDF 大小、投递方式（URL / base64）共同决定，不需要手填固定值。
- **PDF 密码**：默认每个 PDF 随机生成 8 位打开密码并在上传后单独告知；也可配置固定密码或关闭加密。加密失败时不会发出未加密文件。
- **跨容器投递**：AstrBot 与协议端分容器时，通过文件服务 URL 发送；不可用时回退 `base64://`（单文件 ≤256 MiB），URL 失败会自动重试 base64。
- **章节缓存**：已完成的章节 PDF 保存在插件数据目录，重复处理同一本子时只补缺失章节；按最近使用时间淘汰旧缓存。
- **`--no-cache`**：本次任务跳过缓存读取、重新抓取，完成后刷新缓存；可和任一章节选择参数组合使用。
- **`/jm help`**：帮助优先渲染成图片发送，图片渲染失败时回退为文字版。
- **群聊白/黑名单**：可按群号限制可用范围；白名单非空时仅名单内群可用，黑名单优先；两者留空表示所有群可用。
- **配置分组**：基本设置、群聊名单、分卷回退、上传设置、章节缓存、下载与网络。

## 环境要求

- AstrBot `>= 4.27.1, < 5`（已在 4.29.1 验证）
- aiocqhttp（OneBot v11）适配器；协议端为 LLBot / NapCat 等
- Python 3.10+
- 运行依赖：`jmcomic>=2.7.0,<3`、`img2pdf>=0.5.1`、`pypdf[crypto]>=5.0`、`py7zr>=0.20`、`pyzipper>=0.3`

## 安装与重载

1. 把本仓库目录放到 AstrBot 的 `data/plugins/astrbot_jmdownloader/`（目录名必须是合法 Python 标识符）。
2. 在 AstrBot 中安装 `requirements.txt` 中的依赖。
3. 通过 AstrBot 插件管理页或重启 AstrBot 加载插件，确认日志出现 `Plugin astrbot_jmdownloader (<版本>)`。
4. 确认 OneBot v11 适配器已连接、协议端已连上 AstrBot 的反向 WebSocket。

## 使用

### 基础命令

```text
/jm <漫画ID>                 整本
/jm <漫画ID> <章节号>        只处理指定章节
/jm <漫画ID> --head 3        前 3 章
/jm <漫画ID> --tail 3        最后 3 章
/jm <漫画ID> --latest 3      最新 3 章
/jm <漫画ID> --range 5 3     从第 5 章起 3 章
/jm <漫画ID> --range 5 -1    从第 5 章到最新
/jm <漫画ID> --no-cache      本次跳过缓存
/jm <漫画ID> --type zip      发送 zip 压缩包（也支持 7z/tar/tar.gz/tar.bz2/tar.xz）
/jm <漫画ID> --pre 10       预览：取所选范围第一章前 10 页（按默认格式发送）
/jm <漫画ID> --info         查看本子详情 + 封面（图片发送）
/jm help                     查看帮助
```

漫画 ID 支持 `123456`、`JM123456`、`jm 123456` 等写法；章节号支持 `3`、`第3章`、`ch3` 等写法。

### 参数规则

- 以 `--` 开头的开关可以放在 `/jm` 之后的任意位置；未知开关会返回参数错误。
- 章节号必须是漫画 ID 之后的第二个位置参数（例如 `/jm 123456 3`）。
- `--head/--tail/--latest/--range` 之间只能选一个，且不能和位置章节号同时使用。
- `--no-cache` 可以和上述任一章节选择参数组合，顺序不限。
- `--type <格式>` 选择发送格式，未指定时使用配置 `default_file_type`（默认 `pdf`）；例如 `/jm 123456 --head 3 --type zip`。
- `--pre [N]` 进入预览模式：取所选范围（整本/单章/一条范围）第一章的前 N 页，按 `--type` 或配置 `default_file_type` 发送；N 省略或写 0 时用配置 `preview_pages`（默认 10）。

### 帮助

`/jm help`、`/jm --help`、`/jm -h`、`/jm ?` 或直接发送 `/jm` 都会返回帮助。帮助优先发送图片，失败时发送文字。

### 预览

- `--pre [N]`：取所选范围（整本/单章/一条范围）的**第一章**，抓取前 N 页生成预览文件。
- 输出格式由 `--type` 决定；未写时使用配置 `default_file_type`（默认 `pdf`）。zip/7z/tar 系列会把预览 PDF 打包后发送。
- 密码按配置：PDF 预览走 `encrypt_pdf`/`pdf_password`；zip/7z 压缩包本身加密；tar 系列不支持压缩包密码时会给出提示。
- N 省略、写 `0` 或留空时使用配置 `preview_pages`（默认 10，范围 1-50）。
- 参数顺序任意，可与 `--type`、`--head/--tail/--latest/--range`、位置章节号组合。
- 示例：
  - `/jm 123456 --pre` → 第一章前 10 页（配置值），PDF
  - `/jm 123456 --head 5 --pre 10 --type 7z` → 前 5 章中第一章前 10 页，7z
  - `/jm 123456 --range 10 15 --pre 10 --type 7z` → 从第 10 章起范围内第一章前 10 页，7z
  - `/jm 123456 10 --pre 10 --type zip` → 第 10 章前 10 页，zip

### 发送格式

- `--type pdf`：把选中的章节合并成一个 PDF 发送（配置 `default_file_type=pdf` 时为默认）。
- `--type zip|7z|tar|tar.gz|tar.bz2|tar.xz`：把选中的每一章 PDF 打包成压缩包发送；压缩包内文件名带章节序号，便于定位。
- 不写 `--type` 时使用配置「基本设置 → 默认发送格式」`default_file_type`；显式 `--type` 优先。
- **压缩包密码**：启用 PDF 密码时，zip/7z 压缩包本身会使用同一密码加密；tar 系列没有标准的压缩包密码机制，此时会提示改用 zip/7z 或关闭 PDF 加密。
- 压缩包模式不进入 PDF 的分卷回退：上传失败会直接提示原因；压缩包体积过大时建议缩小章节范围或改用 PDF。
- 7z 需要 `py7zr`，加密 zip 需要 `pyzipper`（均已写入 `requirements.txt`）。

### 章节选择与文件

- 单章直接生成并发送该章 PDF。
- 选择多章时合并为一个 PDF 发送；如果合并后上传失败，会按卷回退方案拆分重试。
- 章节号越界会提示相册的有效范围。
- `--range` 的章数省略时默认 1；`-1` 表示从起始章到最新。

## 订阅更新

订阅一个本子后，插件会按周期检查章节变化，发现新章节后按设置推送。

### 命令方式

```text
/jm 123456 --sub loop 6h --type pdf --push file --notice group realtime
/jm 123456 --sub cron 0 9 * * * --type zip --push notice --notice user 111,222 cron 0 20 * * *
/jm 123456 --sub off
```

- `--sub loop <间隔>`：固定间隔检查，支持 `30m/6h/1d`（最小 60 秒，最大 30 天）
- `--sub cron <5 段表达式>`：按 cron 检查（分 时 日 月 周）
- `--type <格式>`：发现更新后推送的文件格式（默认取配置 `default_file_type`）
- `--push file|notice`：推送更新文件（并附通知）或只发消息通知
- `--notice <group|user> [会话ID,...] <realtime|cron> [cron表达式]`：
  - 不写会话 ID 时使用当前会话；多个会话 ID 用逗号分隔
  - `realtime`：检测到更新后立即推送
  - `cron`：在指定 cron 时间汇总推送，后面需跟 5 段表达式
- 未写的字段使用「订阅设置」配置或订阅管理页的默认值
- `/jm <漫画ID> --sub off` 取消该本子的订阅

### 管理页

AstrBot 插件详情页提供「订阅管理」视图（`views/subscriptions/`）：
- 查看/删除已有订阅
- 新建/编辑订阅（本子 ID、订阅方式、格式、推送方式、通知目标）
- 设置订阅默认值
- 查看当前基础配置摘要（其余配置项仍由 AstrBot「插件配置」页管理）

## 跨容器上传配置

AstrBot 与协议端在同一容器时可直接使用文件路径；分容器部署时协议端看不到 AstrBot 容器内的文件，需要通过 URL 或 base64 传递：

1. 在插件配置「上传设置 → callback_api_base」填写协议端能访问到的 AstrBot 地址（例如 `http://astrbot:6185/`）。
2. 也可以使用 AstrBot 全局回调地址；两者都为空或文件服务注册失败时，插件自动回退 `base64://`。
3. 失败时查看 AstrBot 日志中的上传错误，确认两端网络互通、`/api/file/<token>` 可达。

## 群聊名单说明

- 群号支持多个，用逗号、空格或换行分隔，例如 `123456, 234567`。
- 白名单留空：所有群可用；非空：只有名单内的群可用。
- 黑名单留空：不拉黑任何群；非空：名单内群被禁用，且黑名单优先于白名单。
- 私聊不受群聊名单影响；`allow_group` 关闭时所有群都不可用。

## 分卷回退说明

- 默认开启自动规划：整本单文件上传失败后，根据实际 PDF 体积与章节分布计算卷数和每卷边界。
- 分卷只按章节边界切分，同一章的页面不会分散到两个文件。
- 关闭「自动规划分卷」后才使用 `split_size_mb`（单卷目标体积，0 表示不分卷）与 `split_max_volumes`（卷数上限）。
- 每次规划结果可在 AstrBot 日志中查看（含投递方式与各卷章节范围）。

## 缓存与重新下载

- 缓存目录：AstrBot 插件数据目录下的 `plugin_data/astrbot_jmdownloader/cache/`。
- 缓存对象是章节 PDF；`cache_albums` 控制保留本子数量，0 表示关闭缓存。
- 再次处理同一本子时只下载缺失章节；部分章节失败时已完成部分仍保留，便于续传。
- 使用 `--no-cache` 可让本次任务忽略缓存、重新抓取并刷新缓存。
- 缓存目录可手动删除；删除后下次会重新下载。

## 配置项

| 分组 | 配置 | 说明 |
| --- | --- | --- |
| 基本设置 | `enabled` | 插件总开关，关闭后不响应 `/jm` |
| 基本设置 | `allow_group` | 是否允许群聊触发；关闭后仅私聊可用 |
| 基本设置 | `default_file_type` | 未写 `--type` 时的默认发送格式（pdf/zip/7z/tar/tar.gz/tar.bz2/tar.xz） |
| 基本设置 | `preview_pages` | `--pre` 未指定页数或写 0 时的预览页数（1-50，默认 10） |
| 基本设置 | `encrypt_pdf` | 是否给 PDF 设置打开密码 |
| 基本设置 | `pdf_password` | 固定打开密码；留空则每个文件随机生成（明文保存在配置中） |
| 分卷回退 | `split_auto` | 整本上传失败时是否自动规划分卷 |
| 分卷回退 | `split_size_mb` | 手动模式的单卷目标大小（MiB，0 不分卷） |
| 分卷回退 | `split_max_volumes` | 手动模式的卷数上限（1-20） |
| 上传设置 | `callback_api_base` | 协议端可访问的 AstrBot 地址 |
| 上传设置 | `upload_retries` | 上传失败重试次数（0-5） |
| 章节缓存 | `cache_albums` | 缓存保留的本子数（0-50） |
| 订阅设置 | `sub_default_schedule_kind` | 默认订阅方式：loop / cron |
| 订阅设置 | `sub_default_schedule_value` | 默认间隔或 cron 表达式 |
| 订阅设置 | `sub_default_type` | 默认推送格式 |
| 订阅设置 | `sub_default_push_mode` | 默认推送内容：file / notice |
| 订阅设置 | `sub_default_notice_type` | 默认通知目标：group / user |
| 订阅设置 | `sub_default_notice_sids` | 默认通知会话 ID（逗号分隔，留空=当前会话） |
| 订阅设置 | `sub_default_notice_timing` | 默认通知时机：realtime / cron |
| 订阅设置 | `sub_default_notice_cron` | cron 时机的 5 段表达式 |
| 订阅设置 | `sub_check_interval` | 订阅检查间隔秒数（30-600） |
| 群聊名单 | `group_whitelist` | 群聊白名单：群号用逗号/空格/换行分隔；留空表示不限制 |
| 群聊名单 | `group_blacklist` | 群聊黑名单：群号用逗号/空格/换行分隔；留空表示不拉黑，黑名单优先 |
| 下载与网络 | `cookies` | 站点登录 Cookie（可选，`k=v; k2=v2`） |
| 下载与网络 | `proxy` | HTTP/HTTPS 代理地址（可选） |
| 下载与网络 | `timeout` | 单次请求超时秒数（5-120） |
| 下载与网络 | `retry_times` | 单次请求重试次数（1-10） |
| 下载与网络 | `image_threads` | 章节内图片下载并发数（1-32） |
| 下载与网络 | `photo_threads` | 同时处理的章节数（1-16） |
| 下载与网络 | `max_concurrent` | 同时运行的漫画任务数（1-3） |

## 本地测试

```bash
python3 -m pytest tests -q
```

测试覆盖：命令解析、章节选择、文件名、动态分卷规划、PDF 合成/合并/加密、缓存淘汰、上传 URL/base64 回退、配置解析、许可与合规声明。

## 常见问题

**Q：整本上传失败，收到多卷文件？**
A：整本单文件上传在部分协议端会随机失败（例如大文件 HTTP Highway 超时）。插件会按章节把内容拆成多卷重试，卷数由实际体积计算，日志中会给出规划结果。

**Q：提示超过 base64 上限？**
A：未配置可访问的 AstrBot 地址时只能走 base64；文件过大时请填写 `callback_api_base`，让协议端直接拉取文件服务 URL。

**Q：没有收到 PDF？**
A：确认 OneBot v11 适配器已连接、机器人有上传文件的权限、协议端与 AstrBot 网络互通；再查看 AstrBot 日志中的具体错误。

**Q：提示内容不存在或需要登录？**
A：检查漫画 ID 是否正确；仅登录可见的内容需要配置有效的 Cookie。

**Q：想要固定密码或关闭加密？**
A：在插件配置「基本设置」中填写 `pdf_password`，或关闭 `encrypt_pdf`；固定密码会以明文保存在配置中。

**Q：缓存占用太大？**
A：调低 `cache_albums`（0 表示关闭缓存），或手动删除插件数据目录下的缓存文件夹。

## 来源与许可

本插件在功能设计与部分实现方式上参考了 [CLRedfield/astrbot-plugin-jmdownloader](https://github.com/CLRedfield/astrbot-plugin-jmdownloader)（MIT License，`Copyright (c) 2026 Rosepineux`）。按 MIT 许可要求，本仓库在 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 中保留了其版权与许可声明；本项目与上游作者不存在隶属、合作或背书关系。

本仓库自有代码使用 [MIT License](LICENSE) © 2026 ACAT；运行时依赖（AstrBot、jmcomic、img2pdf、pypdf）的许可说明见 `THIRD_PARTY_NOTICES.md`。

## 合规与免责声明

1. **用途**：仅供技术学习、个人备份与自动化流程研究；不提供、不托管、不分发任何漫画内容，也不包含破解或绕过目标站点付费/技术保护措施的功能。
2. **著作权**：目标站点的漫画、图片及相关内容版权归各自权利人所有；请勿将本插件用于侵权下载、传播、商业使用或再分发，下载内容仅限个人合法备份。
3. **法律法规**：使用前请确认并遵守所在国家/地区适用的法律法规（包括著作权、网络安全、个人信息保护以及内容分级/成人内容管理规定）和目标站点的服务条款。
4. **成人内容**：目标站点可能包含成人内容；本项目不面向未成年人，请确保使用者达到法定年龄且所在地区允许访问此类内容。
5. **凭据**：Cookie、代理、固定 PDF 密码等以明文保存在 AstrBot 配置/数据目录中，请自行妥善保管；随机密码不写日志。
6. **数据**：插件不上传用户数据、不含遥测；只在本机临时目录与插件缓存目录保存任务所需文件，并通过 AstrBot/OneBot 发送到指定会话。
7. **责任**：使用本插件产生的风险与后果由使用者自行承担；作者不对因使用或无法使用本插件造成的损失负责。若当地法律或使用场景不允许上述行为，请停止使用。
8. **非法律意见**：以上为一般性合规说明，不构成法律意见；用于商业、公开服务或大规模分发前，请咨询专业法律人士并独立核查依赖许可。

## License

[MIT](LICENSE) © 2026 ACAT。第三方依赖许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
