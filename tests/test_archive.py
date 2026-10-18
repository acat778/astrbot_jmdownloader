"""压缩包生成测试（zip / tar 系列 / 7z）。"""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from astrbot_jmdownloader.core.archive import (
    archive_suffix,
    build_archive,
    normalize_archive_format,
)
from astrbot_jmdownloader.core.errors import ArchiveBuildError


def _members(tmp_path):
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"%PDF-1.4 first")
    second.write_bytes(b"%PDF-1.4 second")
    return [(first, "001-a.pdf"), (second, "002-b.pdf")]


def test_normalize_archive_format():
    assert normalize_archive_format("ZIP") == "zip"
    assert normalize_archive_format("tgz") == "tar.gz"
    assert normalize_archive_format("tar.bz") == "tar.bz2"
    with pytest.raises(ArchiveBuildError):
        normalize_archive_format("rar")
    assert archive_suffix("tar.gz") == ".tar.gz"


def test_build_zip(tmp_path):
    output = tmp_path / "out.zip"
    build_archive(_members(tmp_path), output, "zip")
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["001-a.pdf", "002-b.pdf"]


def test_build_tar_gz(tmp_path):
    output = tmp_path / "out.tar.gz"
    build_archive(_members(tmp_path), output, "tar.gz")
    assert output.exists()
    with tarfile.open(output, "r:gz") as archive:
        assert archive.getnames() == ["001-a.pdf", "002-b.pdf"]


def test_build_zip_with_password(tmp_path):
    pyzipper = pytest.importorskip("pyzipper")
    output = tmp_path / "out.zip"
    build_archive(_members(tmp_path), output, "zip", "Secret123")
    with pyzipper.AESZipFile(output) as archive:
        archive.setpassword(b"Secret123")
        assert archive.namelist() == ["001-a.pdf", "002-b.pdf"]


def test_tar_with_password_raises(tmp_path):
    with pytest.raises(ArchiveBuildError):
        build_archive(_members(tmp_path), tmp_path / "out.tar.gz", "tar.gz", "x")


def test_build_7z_with_password(tmp_path):
    py7zr = pytest.importorskip("py7zr")
    output = tmp_path / "out.7z"
    build_archive(_members(tmp_path), output, "7z", "Secret123")
    with py7zr.SevenZipFile(output, "r", password="Secret123") as archive:
        assert sorted(archive.getnames()) == ["001-a.pdf", "002-b.pdf"]


def test_build_7z_when_available(tmp_path):
    pytest.importorskip("py7zr")
    output = tmp_path / "out.7z"
    build_archive(_members(tmp_path), output, "7z")
    assert output.exists() and output.stat().st_size > 0


def test_empty_members_raise(tmp_path):
    with pytest.raises(ArchiveBuildError):
        build_archive([], tmp_path / "out.zip", "zip")
