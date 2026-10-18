"""把章节 PDF 打包为 zip / 7z / tar 系列压缩包。"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Iterable

from .errors import ArchiveBuildError

ARCHIVE_SUFFIXES = {
    "zip": ".zip",
    "7z": ".7z",
    "tar": ".tar",
    "tar.gz": ".tar.gz",
    "tar.bz2": ".tar.bz2",
    "tar.xz": ".tar.xz",
}

_FORMAT_ALIASES = {
    "tgz": "tar.gz",
    "gz": "tar.gz",
    "tbz2": "tar.bz2",
    "tar.bz": "tar.bz2",
    "txz": "tar.xz",
}

_TAR_MODES = {
    "tar": "w",
    "tar.gz": "w:gz",
    "tar.bz2": "w:bz2",
    "tar.xz": "w:xz",
}

OUTPUT_TYPE_HINT = "pdf/zip/7z/tar/tar.gz/tar.bz2/tar.xz"


def normalize_archive_format(raw: str) -> str:
    """把用户输入规范成受支持的压缩格式；不支持时抛 ArchiveBuildError。"""
    text = str(raw or "").strip().lower().lstrip(".")
    text = _FORMAT_ALIASES.get(text, text)
    if text not in ARCHIVE_SUFFIXES:
        raise ArchiveBuildError(
            f"不支持的压缩格式：{raw or '(空)'}；可用：{OUTPUT_TYPE_HINT}"
        )
    return text


def archive_suffix(fmt: str) -> str:
    """返回压缩格式对应的文件后缀（如 ``.tar.gz``）。"""
    return ARCHIVE_SUFFIXES[normalize_archive_format(fmt)]


def build_archive(
    members: Iterable[tuple[Path, str]],
    output: Path,
    fmt: str,
    password: str = "",
) -> Path:
    """按 ``(源文件, 包内文件名)`` 列表生成压缩包，返回输出路径。

    ``password`` 非空时给压缩包本身加密：zip 使用 AES、7z 使用内建加密；
    tar 系列没有标准压缩包密码机制，传 password 会明确报错。
    """
    items = [(Path(source), str(name)) for source, name in members]
    if not items:
        raise ArchiveBuildError("没有可打包的章节 PDF")
    fmt = normalize_archive_format(fmt)
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "zip":
        _build_zip(items, output, password)
    elif fmt == "7z":
        _build_7z(items, output, password)
    else:
        if password:
            raise ArchiveBuildError(
                "tar 系列压缩包不支持设置密码；请改用 --type zip/7z，"
                "或关闭 PDF 加密后再使用 tar 系列。"
            )
        with tarfile.open(output, _TAR_MODES[fmt]) as archive:
            for source, name in items:
                archive.add(source, arcname=name)
    return output


def _build_zip(
    items: list[tuple[Path, str]], output: Path, password: str
) -> None:
    if not password:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for source, name in items:
                archive.write(source, arcname=name)
        return
    try:
        import pyzipper
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ArchiveBuildError(
            "zip 压缩包加密需要安装 pyzipper（pip install pyzipper）"
        ) from exc
    with pyzipper.AESZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(password.encode("utf-8"))
        for source, name in items:
            archive.write(source, arcname=name)


def _build_7z(
    items: list[tuple[Path, str]], output: Path, password: str
) -> None:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ArchiveBuildError(
            "生成 7z 压缩包需要安装 py7zr（pip install py7zr）"
        ) from exc
    with py7zr.SevenZipFile(output, "w", password=password or None) as archive:
        for source, name in items:
            archive.write(source, arcname=name)
