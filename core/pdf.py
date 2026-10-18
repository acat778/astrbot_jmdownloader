"""PDF 生成、合并、加密与校验。

章节 PDF 直接调用 ``img2pdf`` 生成：jmcomic 负责下载并解码图片，
本模块按自然顺序收集章节图片后交给 img2pdf，得到可单独缓存/上传的章节 PDF。
整本或分卷 PDF 用 ``pypdf`` 合并；加密同样走 pypdf（AES-256，缺
``cryptography`` 时回退 RC4-128）。
"""

from __future__ import annotations

import os
import re
from io import BytesIO
from pathlib import Path
from typing import Sequence

import img2pdf

from .errors import PdfBuildError

PDF_MAGIC = b"%PDF-"
MIN_PDF_BYTES = 1024
IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}

_NUMERIC_PREFIX = re.compile(r"(\d+)")


def image_sort_key(path: Path) -> tuple:
    """图片自然排序键：``00009.jpg`` 排在 ``00010.jpg`` 前面。"""
    stem = path.stem
    numbers = _NUMERIC_PREFIX.findall(stem)
    if numbers:
        return (0, int(numbers[0]), stem)
    return (1, 0, stem)


def list_chapter_images(image_dir: Path) -> list[Path]:
    """按页序返回章节图片（忽略隐藏文件与非图片文件）。"""
    if not image_dir.is_dir():
        return []
    images = [
        item
        for item in image_dir.iterdir()
        if item.is_file()
        and not item.name.startswith(".")
        and item.suffix.lower() in IMAGE_SUFFIXES
    ]
    images.sort(key=image_sort_key)
    return images


def validate_pdf(pdf_path: Path) -> None:
    """确认文件存在、以 PDF 魔数开头且体积合理。"""
    try:
        with pdf_path.open("rb") as stream:
            magic = stream.read(len(PDF_MAGIC))
            stream.seek(0, os.SEEK_END)
            total = stream.tell()
    except OSError as exc:
        raise PdfBuildError(f"PDF 文件不存在：{pdf_path.name}") from exc
    if magic != PDF_MAGIC:
        raise PdfBuildError(f"生成的文件不是有效 PDF：{pdf_path.name}")
    if total < MIN_PDF_BYTES:
        raise PdfBuildError(
            f"生成的 PDF 过小（{total} 字节）：{pdf_path.name}"
        )


def _atomic_write(target: Path, payload: bytes) -> None:
    """先写同目录临时文件再替换，避免中断留下半截 PDF。"""
    staging = target.with_name(f".{target.name}.part")
    try:
        staging.write_bytes(payload)
        os.replace(staging, target)
    finally:
        if staging.is_file():
            try:
                staging.unlink()
            except OSError:
                pass


def _writer_payload(pdf_writer: object) -> bytes:
    """把 pypdf writer 序列化到内存，交给原子写落盘。"""
    buffer = BytesIO()
    pdf_writer.write(buffer)
    return buffer.getvalue()


def build_chapter_pdf(image_paths: Sequence[Path], output_path: Path) -> int:
    """把章节图片合成为 PDF，返回页数。

    图片顺序由调用方（:func:`list_chapter_images`）保证；输出先写临时文件
    再原子替换，避免中断留下半截缓存。
    """
    if not image_paths:
        raise PdfBuildError("章节没有可用的图片，无法生成 PDF")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # first_frame_only=True：多帧图片（GIF/MPO 等）每张只取首帧一页，
    # 避免一章里出现成倍页数。
    payload = img2pdf.convert(
        [str(path) for path in image_paths],
        first_frame_only=True,
    )
    if not payload.startswith(PDF_MAGIC):
        raise PdfBuildError("img2pdf 输出不是有效 PDF")
    _atomic_write(output_path, payload)
    validate_pdf(output_path)
    return len(image_paths)


def merge_pdfs(parts: Sequence[Path], output_path: Path) -> int:
    """按章节顺序拼接为整本/单卷 PDF，返回总页数。"""
    if not parts:
        raise PdfBuildError("没有可合并的章节 PDF")
    from pypdf import PdfWriter

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_writer = PdfWriter()
    try:
        for shard in parts:
            try:
                pdf_writer.append(str(shard))
            except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
                raise PdfBuildError(f"合并章节 PDF 失败：{shard.name}") from exc
        payload = _writer_payload(pdf_writer)
    finally:
        pdf_writer.close()
    _atomic_write(output_path, payload)
    validate_pdf(output_path)
    return pdf_page_count(output_path)


def encrypt_pdf(source_path: Path, output_path: Path, password: str) -> Path:
    """用 pypdf 给 PDF 加打开密码，返回加密后的新文件。"""
    from pypdf import PdfReader, PdfWriter

    source_reader = PdfReader(str(source_path))
    pdf_writer = PdfWriter(clone_from=source_reader)
    try:
        try:
            pdf_writer.encrypt(password, password, algorithm="AES-256")
        except Exception:  # noqa: BLE001 - 缺 cryptography 时回退
            pdf_writer.encrypt(password, password, algorithm="RC4-128")
        payload = _writer_payload(pdf_writer)
    finally:
        pdf_writer.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_path, payload)
    validate_pdf(output_path)
    return output_path


def pdf_page_count(pdf_path: Path) -> int:
    """读取 PDF 实际页数（用于日志与规划统计；读取失败返回 0）。"""
    try:
        from pypdf import PdfReader

        document = PdfReader(str(pdf_path))
        return len(document.pages)
    except Exception:  # noqa: BLE001 - 统计失败不阻断任务
        return 0
