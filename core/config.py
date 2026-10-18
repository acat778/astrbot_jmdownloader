"""插件配置快照与 jmcomic 选项构造。

AstrBot 下发的配置对象是可变 dict，这里每次命令开始时生成不可变快照，
避免长任务期间配置被 WebUI 改到一半。所有数值都做范围收敛，
避免用户填 0/负数/超大值把任务拖垮。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .archive import normalize_archive_format
from .errors import ArchiveBuildError
from typing import Any, Mapping

DEFAULT_PLUGIN_NAME = "astrbot_jmdownloader"

# 宽松布尔解析：字符串形式的常见真值与中文“开启”都算真
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "开启"})


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE_WORDS


def _as_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    candidate = default if raw is None else raw
    try:
        parsed = int(candidate)
    except (TypeError, ValueError):
        return default
    return minimum if parsed < minimum else maximum if parsed > maximum else parsed


def flatten_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把配置压平成 key -> value。

    新版 `_conf_schema.json` 用 object 分组做卡片展示，配置对象因此嵌套；
    旧版是扁平结构。这里统一压平，两种形态都能读。
    """
    flat: dict[str, Any] = {}

    def walk(mapping: Mapping[str, Any]) -> None:
        for key, value in mapping.items():
            if isinstance(value, dict):
                walk(value)
            else:
                flat[str(key)] = value

    walk(raw or {})
    return flat


def parse_cookies(raw: Any) -> dict[str, str]:
    """解析 ``k=v; k2=v2`` 形式的 Cookie 头。"""
    jar: dict[str, str] = {}
    for item in str(raw or "").split(";"):
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        key = key.strip()
        if key:
            jar[key] = val.strip()
    return jar


def parse_default_file_type(raw: Any) -> str:
    """解析默认发送格式；空值/非法值回退为 pdf。"""
    text = str(raw or "").strip().lower().lstrip(".")
    if not text or text == "pdf":
        return "pdf"
    try:
        return normalize_archive_format(text)
    except ArchiveBuildError:
        return "pdf"


def parse_group_ids(raw: Any) -> frozenset[int]:
    """解析群号名单：支持逗号、中文逗号、分号、空格与换行分隔。"""
    numbers: set[int] = set()
    for token in re.split(r"[,，;；\s]+", str(raw or "")):
        token = token.strip()
        if token.isdigit():
            numbers.add(int(token))
    return frozenset(numbers)


def normalize_proxy(raw: Any) -> str:
    """给缺协议头的代理地址补上 ``http://``。"""
    address = str(raw or "").strip()
    if not address:
        return ""
    if "://" in address:
        return address
    return "http://" + address


@dataclass(frozen=True)
class PluginConfig:
    """一次任务使用的配置快照。"""

    enabled: bool = True
    allow_group: bool = True
    group_whitelist: frozenset[int] = frozenset()
    group_blacklist: frozenset[int] = frozenset()
    encrypt_pdf: bool = True
    pdf_password: str = ""
    callback_api_base: str = ""
    split_auto: bool = True
    split_size_mb: int = 100
    split_max_volumes: int = 8
    upload_retries: int = 2
    cache_albums: int = 5
    preview_images: int = 10
    default_file_type: str = "pdf"
    cookies: str = ""
    proxy: str = ""
    timeout: int = 20
    retry_times: int = 3
    image_threads: int = 16
    photo_threads: int = 4
    max_concurrent: int = 1

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PluginConfig":
        source = flatten_config(raw)
        return cls(
            enabled=_as_bool(source.get("enabled"), True),
            allow_group=_as_bool(source.get("allow_group"), True),
            group_whitelist=parse_group_ids(source.get("group_whitelist")),
            group_blacklist=parse_group_ids(source.get("group_blacklist")),
            encrypt_pdf=_as_bool(source.get("encrypt_pdf"), True),
            pdf_password=str(source.get("pdf_password") or "").strip(),
            callback_api_base=str(source.get("callback_api_base") or "").strip(),
            split_auto=_as_bool(source.get("split_auto"), True),
            split_size_mb=_as_int(source.get("split_size_mb"), 100, 0, 500),
            split_max_volumes=_as_int(
                source.get("split_max_volumes"), 8, 1, 20
            ),
            upload_retries=_as_int(source.get("upload_retries"), 2, 0, 5),
            cache_albums=_as_int(source.get("cache_albums"), 5, 0, 50),
            preview_images=_as_int(
                source.get("preview_images"), 10, 1, 50
            ),
            default_file_type=parse_default_file_type(
                source.get("default_file_type")
            ),
            cookies=str(source.get("cookies") or ""),
            proxy=str(source.get("proxy") or ""),
            timeout=_as_int(source.get("timeout"), 20, 5, 120),
            retry_times=_as_int(source.get("retry_times"), 3, 1, 10),
            image_threads=_as_int(source.get("image_threads"), 16, 1, 32),
            photo_threads=_as_int(source.get("photo_threads"), 4, 1, 16),
            max_concurrent=_as_int(source.get("max_concurrent"), 1, 1, 3),
        )

    def group_access(self, group_id: str | int | None) -> str:
        """判断群聊访问级别：ok / disabled / blacklist / whitelist。"""
        if not self.allow_group:
            return "disabled"
        try:
            gid = int(str(group_id).strip())
        except (TypeError, ValueError):
            return "ok"
        if gid in self.group_blacklist:
            return "blacklist"
        if self.group_whitelist and gid not in self.group_whitelist:
            return "whitelist"
        return "ok"

    @property
    def split_enabled(self) -> bool:
        """整本上传失败后是否允许按章分卷回退。"""
        if self.split_auto:
            return True
        return self.split_size_mb > 0

    def client_option_dict(self) -> dict[str, Any]:
        """构造 jmcomic 的 client/download 配置（不含 dir_rule）。"""
        request_meta: dict[str, Any] = {"timeout": self.timeout}
        parsed_cookies = parse_cookies(self.cookies) if self.cookies else {}
        if parsed_cookies:
            request_meta.update({"cookies": parsed_cookies})
        proxy_value = normalize_proxy(self.proxy)
        if proxy_value:
            request_meta["proxies"] = {"http": proxy_value, "https": proxy_value}

        threading_cfg = {"image": self.image_threads, "photo": self.photo_threads}
        image_cfg = {"decode": True, "suffix": ".jpg"}
        download_cfg = {
            "cache": True,
            "image": image_cfg,
            "threading": threading_cfg,
        }
        client_cfg = {
            "impl": "api",
            "retry_times": self.retry_times,
            "postman": {"meta_data": request_meta},
        }
        return {"download": download_cfg, "client": client_cfg}

    def jmcomic_option_dict(self, base_dir: Path) -> dict[str, Any]:
        """构造一个把章节图片平铺到 ``base_dir/<pid>/`` 的 jmcomic 选项。"""
        option = self.client_option_dict()
        option["dir_rule"] = {"rule": "Bd/Pid", "base_dir": str(base_dir)}
        return option
