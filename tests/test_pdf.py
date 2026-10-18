"""PDF 合成、合并、加密与图片排序测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_jmdownloader.core.errors import PdfBuildError
from astrbot_jmdownloader.core.pdf import (
    build_chapter_pdf,
    encrypt_pdf,
    list_chapter_images,
    merge_pdfs,
    pdf_page_count,
    validate_pdf,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _make_images(directory: Path, count: int = 3) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(1, count + 1):
        path = directory / f"{index:05d}.png"
        Image.new("RGB", (80, 120), color=(index * 30, 60, 90)).save(path)
        paths.append(path)
    return paths


def test_list_chapter_images_natural_order(tmp_path):
    images = _make_images(tmp_path / "caps", count=12)
    assert [path.name for path in list_chapter_images(tmp_path / "caps")] == [
        f"{index:05d}.png" for index in range(1, 13)
    ]
    (tmp_path / "caps" / ".hidden.png").write_bytes(b"x")
    (tmp_path / "caps" / "notes.txt").write_bytes(b"x")
    filtered = list_chapter_images(tmp_path / "caps")
    assert len(filtered) == len(images)


def test_build_chapter_pdf_and_merge(tmp_path):
    first_images = _make_images(tmp_path / "one", count=2)
    second_images = _make_images(tmp_path / "two", count=3)
    first_pdf = tmp_path / "one.pdf"
    second_pdf = tmp_path / "two.pdf"

    assert build_chapter_pdf(first_images, first_pdf) == 2
    assert build_chapter_pdf(second_images, second_pdf) == 3
    validate_pdf(first_pdf)
    assert pdf_page_count(first_pdf) == 2

    merged = tmp_path / "merged.pdf"
    assert merge_pdfs([first_pdf, second_pdf], merged) == 5
    assert pdf_page_count(merged) == 5


def test_build_chapter_pdf_rejects_empty(tmp_path):
    with pytest.raises(PdfBuildError):
        build_chapter_pdf([], tmp_path / "empty.pdf")


def test_validate_pdf_rejects_non_pdf(tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf" * 200)
    with pytest.raises(PdfBuildError):
        validate_pdf(fake)
    missing = tmp_path / "missing.pdf"
    with pytest.raises(PdfBuildError):
        validate_pdf(missing)


def test_encrypt_pdf_roundtrip(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    images = _make_images(tmp_path / "imgs", count=2)
    source = tmp_path / "source.pdf"
    build_chapter_pdf(images, source)

    encrypted = tmp_path / "encrypted.pdf"
    assert encrypt_pdf(source, encrypted, "Abc23456") == encrypted
    assert encrypted.stat().st_size > 0

    reader = PdfReader(str(encrypted))
    assert reader.is_encrypted is True
    assert reader.decrypt("Abc23456")
    assert len(reader.pages) == 2

    wrong = PdfReader(str(encrypted))
    try:
        assert not wrong.decrypt("wrong-password")
    except Exception:
        pass
