"""跨容器文件投递。

AstrBot 与协议端（LLBot / NapCat 等）分容器部署时，协议端读不到 AstrBot
容器内的临时文件路径，直接传路径会得到 ``retcode 1200 未知文件类型或路径
不存在``。因此先把 PDF 注册到 AstrBot 文件服务，再用协议端可达的地址生成
下载 URL；两者都拿不到、或注册失败时回退 ``base64://``（受体积上限保护）。
URL 方式上传失败时同样会用 base64 再试一次。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from .errors import UploadConfigError
from .models import MIB

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行时不依赖 AstrBot
    from astrbot.api.event import AstrMessageEvent


FILE_URL_TTL_SECONDS = 600

# base64 需要把整份 PDF 塞进反向 WS 的 JSON 报文，协议端（Node/V8）单字符串
# 上限约 512 MiB 字符，对应原始文件约 384 MiB；这里按 256 MiB 保守拒绝。
MAX_BASE64_BYTES = 256 * MIB

_BASE64_PREFIX = "base64://"
_URL_TEMPLATE = "{base}/api/file/{token}"
_BUDGET_HINT = "请在插件配置填写「AstrBot 可访问地址」，或调小分卷体积。"


def resolve_callback_base(plugin_value: str) -> str:
    """按「插件配置 → AstrBot 全局配置」顺序取协议端可达的 AstrBot 地址。"""
    base = str(plugin_value or "").strip()
    if base:
        return base
    try:
        from astrbot.core import astrbot_config

        return str(astrbot_config.get("callback_api_base") or "").strip()
    except Exception:  # noqa: BLE001 - 兼容测试环境/旧版本
        logger.warning("读取 AstrBot 全局回调地址失败", exc_info=True)
        return ""


def base64_file_uri(pdf_path: Path) -> str:
    """把 PDF 编码成 ``base64://``；超过兜底上限直接拒绝。"""
    byte_count = pdf_path.stat().st_size
    if byte_count > MAX_BASE64_BYTES:
        problem = (
            f"PDF 体积 {byte_count / MIB:.1f} MiB 超过 base64 兜底上限 "
            f"{MAX_BASE64_BYTES // MIB} MiB：{_BUDGET_HINT}"
        )
        raise UploadConfigError(problem)
    payload = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    return _BASE64_PREFIX + payload


async def _register_file_token(pdf_path: Path) -> str:
    """把 PDF 注册到 AstrBot 文件服务并返回下载 token。"""
    from astrbot.core import file_token_service

    absolute = str(pdf_path.resolve())
    handle = await file_token_service.register_file(
        absolute, timeout=FILE_URL_TTL_SECONDS, single_use=False
    )
    return str(handle)


async def build_upload_uri(pdf_path: Path, callback_base: str) -> str:
    """生成协议端可读取的文件 URI：文件服务 URL 优先，base64 兜底。"""
    address = str(callback_base or "").strip()
    if address:
        try:
            token = await _register_file_token(pdf_path)
        except Exception:  # noqa: BLE001 - 注册失败时回退 base64
            logger.warning(
                "注册 AstrBot 文件服务 URL 失败，改用 base64:// 上传", exc_info=True
            )
        else:
            return _URL_TEMPLATE.format(base=address.rstrip("/"), token=token)
    return base64_file_uri(pdf_path)


async def _call_onebot_upload(
    event: "AstrMessageEvent",
    uri: str,
    display_name: str,
) -> object:
    """按会话类型调用 OneBot 的群文件/私聊文件上传动作。"""
    group = event.get_group_id()
    if group:
        endpoint, recipient = "upload_group_file", ("group_id", int(group))
    else:
        endpoint, recipient = (
            "upload_private_file",
            ("user_id", int(event.get_sender_id())),
        )
    arguments = {"file": uri, "name": display_name, recipient[0]: recipient[1]}
    return await event.bot.api.call_action(endpoint, **arguments)


async def upload_pdf(
    event: "AstrMessageEvent",
    pdf_path: Path,
    file_name: str,
    callback_base: str,
) -> object:
    """上传 PDF；文件服务 URL 失败时自动改用 base64 再试一次。"""
    location = await build_upload_uri(pdf_path, callback_base)
    try:
        return await _call_onebot_upload(event, location, file_name)
    except Exception as exc:  # noqa: BLE001 - URL 上传失败时尝试 base64 兜底
        if isinstance(exc, UploadConfigError) or location.startswith(_BASE64_PREFIX):
            raise
        logger.warning("协议端上传文件服务 URL 失败，改用 base64:// 重试：%s", location)
        return await _call_onebot_upload(
            event, base64_file_uri(pdf_path), file_name
        )
