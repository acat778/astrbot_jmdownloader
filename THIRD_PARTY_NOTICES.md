# 第三方组件与来源许可声明（THIRD-PARTY NOTICES）

本仓库的插件在功能设计与部分实现方式上参考了下列 MIT 项目；按 MIT 许可要求，本文件保留其版权与许可声明。本仓库不包含该项目的完整源码，也不声称与其作者存在隶属、合作或背书关系。

## 1. 参考来源

- 项目：[CLRedfield/astrbot-plugin-jmdownloader](https://github.com/CLRedfield/astrbot-plugin-jmdownloader)
- 参考版本：1.0.3（commit `9172123`，2026-08-10）
- 上游许可：MIT License，版权声明为 `Copyright (c) 2026 Rosepineux`（上游 LICENSE 原文如下）。

```text
MIT License

Copyright (c) 2026 Rosepineux

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

> 上游 `metadata.yaml` 的作者署名为 `CLRedfield`，上游 `LICENSE` 的版权行为 `Rosepineux`；本文件按上游 LICENSE 原文保留版权行，并同时标注项目作者，避免歧义。

## 2. 运行时依赖（不在本仓库中分发）

本插件通过 `requirements.txt` 声明依赖，由 AstrBot 在运行环境中安装；本仓库不包含、也不修改下列项目的源代码。

| 依赖 | 许可 | 项目地址 | 说明 |
| --- | --- | --- | --- |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | AGPL-3.0-or-later | https://github.com/AstrBotDevs/AstrBot | 插件宿主应用。本插件仅通过 AstrBot 公开插件 API 运行，不修改或内嵌 AstrBot 源码；AstrBot 本身的再分发/修改需遵守其 AGPL-3.0-or-later 条款。 |
| [jmcomic](https://github.com/hect0x7/JMComic-Crawler-Python) | MIT License（Copyright (c) 2023 hect0x7） | https://github.com/hect0x7/JMComic-Crawler-Python | 用于访问相册元数据、下载并解码章节图片。 |
| [img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) | LGPL-3.0 | https://gitlab.mister-muffin.de/josch/img2pdf | 调用其公开 API 将章节图片合成为 PDF；未修改其源码。 |
| [py7zr](https://github.com/miurahr/py7zr) | LGPL-2.1-or-later | https://github.com/miurahr/py7zr | 生成 7z 压缩包。 |
| [pyzipper](https://github.com/danifus/pyzipper) | MIT License | https://github.com/danifus/pyzipper | 生成带 AES 密码的 zip 压缩包。 |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | https://github.com/py-pdf/pypdf | 用于 PDF 合并、页数统计与密码加密。 |

> 依赖的实际许可证文本随对应 Python 包一起分发（见各包 `dist-info/LICENSE*`）。若将本插件与依赖打包成单一发行物再分发，需要同时满足对应依赖的许可条款（例如 LGPL-3.0 的源码/替换要求、BSD-3-Clause 的版权与免责声明保留要求、AGPL-3.0 针对 AstrBot 的要求）。

## 3. 本仓库许可

本项目自有代码使用 [MIT License](LICENSE)（Copyright (c) 2026 ACAT）。
