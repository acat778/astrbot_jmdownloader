"""jmcomic 适配层测试（不联网，桩掉下载函数）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

jmcomic = pytest.importorskip("jmcomic")
from jmcomic.jm_exception import (  # noqa: E402
    MissingAlbumPhotoException,
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)
from PIL import Image  # noqa: E402

from astrbot_jmdownloader.core.config import PluginConfig  # noqa: E402
from astrbot_jmdownloader.core.errors import ChapterDownloadError  # noqa: E402
from astrbot_jmdownloader.core.jm_source import (  # noqa: E402
    JmSource,
    _episode_fields,
    _manifest_image_paths,
    _ordered_chapter_images,
)


def test_episode_fields_variants():
    assert _episode_fields(("123", "1", "标题")) == ("123", "1", "标题")
    assert _episode_fields(("123", "1", "标题", "2026-01-01")) == (
        "123",
        "1",
        "标题",
    )
    assert _episode_fields(("123",)) == ("123", "", "")
    assert _episode_fields("123") == ("123", "", "")


def test_option_built_from_config(tmp_path):
    source = JmSource(
        PluginConfig(
            cookies="a=b",
            proxy="127.0.0.1:7890",
            timeout=31,
            retry_times=5,
            image_threads=3,
            photo_threads=2,
        )
    )
    option = jmcomic.JmOption.construct(
        source.config.jmcomic_option_dict(tmp_path)
    )
    assert option.dir_rule.rule_dsl == "Bd/Pid"
    assert option.dir_rule.base_dir == str(tmp_path)
    assert option.client.retry_times == 5
    assert option.download.threading.image == 3
    assert option.download.threading.photo == 2
    assert option.client.postman.meta_data["timeout"] == 31


def test_download_chapter_builds_pdf(monkeypatch, tmp_path):
    images_root = tmp_path / "images"
    pdf_path = tmp_path / "chapters" / "123.pdf"

    def fake_download_photo(photo_id, option):
        directory = images_root / str(photo_id)
        directory.mkdir(parents=True)
        image_paths = []
        for index in (1, 2):
            path = directory / f"{index:05d}.png"
            Image.new("RGB", (60, 90), color=(index, 2, 3)).save(path)
            image_paths.append(str(path))
        # jmcomic 2.7.8: download_photo 返回 DownloadResult(detail, downloader)
        return SimpleNamespace(
            detail=SimpleNamespace(save_path=str(directory)),
            manifest=SimpleNamespace(image_filepath_list=image_paths),
        )

    monkeypatch.setattr(jmcomic, "download_photo", fake_download_photo)
    source = JmSource(PluginConfig())
    artifact = source.download_chapter("123", images_root, pdf_path)

    assert artifact.pid == "123"
    assert artifact.page_count == 2
    assert artifact.size_bytes > 0
    assert artifact.image_bytes > 0
    assert artifact.pdf_path == pdf_path
    assert pdf_path.is_file()
    assert not (images_root / "123").exists()  # 原始图片已清理


def test_download_chapter_maps_exceptions(monkeypatch, tmp_path):
    def fake_download_photo(photo_id, option):
        raise MissingAlbumPhotoException("missing", {})

    monkeypatch.setattr(jmcomic, "download_photo", fake_download_photo)
    source = JmSource(PluginConfig())

    with pytest.raises(ChapterDownloadError) as error:
        source.download_chapter("123", tmp_path / "images", tmp_path / "x.pdf")
    assert error.value.pid == "123"


@pytest.mark.parametrize(
    "exception",
    [
        PartialDownloadFailedException("partial", {}),
        RequestRetryAllFailException("retry", {}),
    ],
)
def test_download_chapter_maps_other_jmcomic_errors(
    monkeypatch, tmp_path, exception
):
    def fake_download_photo(photo_id, option):
        raise exception

    monkeypatch.setattr(jmcomic, "download_photo", fake_download_photo)
    source = JmSource(PluginConfig())
    with pytest.raises(ChapterDownloadError):
        source.download_chapter("123", tmp_path / "images", tmp_path / "x.pdf")


def test_ordered_images_follow_manifest(tmp_path):
    directory = tmp_path / "chapter"
    directory.mkdir()
    for index in (1, 2, 3):
        Image.new("RGB", (10, 10), color=(index, 0, 0)).save(
            directory / f"{index:05d}.png"
        )
    # manifest 页序与文件名顺序不同，应优先采用 manifest
    manifest = [
        directory / "00003.png",
        directory / "00001.png",
        directory / "00002.png",
    ]
    ordered = _ordered_chapter_images(directory, manifest)
    assert [path.name for path in ordered] == [
        "00003.png",
        "00001.png",
        "00002.png",
    ]


def test_ordered_images_falls_back_to_natural_order(tmp_path):
    directory = tmp_path / "chapter"
    directory.mkdir()
    for index in (10, 2, 1):
        Image.new("RGB", (10, 10), color=(index, 0, 0)).save(
            directory / f"{index:05d}.png"
        )
    ordered = _ordered_chapter_images(directory, [])
    assert [path.name for path in ordered] == [
        "00001.png",
        "00002.png",
        "00010.png",
    ]


def test_manifest_image_paths_filters_missing(tmp_path):
    existing = tmp_path / "00001.jpg"
    existing.write_bytes(b"x")
    result = SimpleNamespace(
        manifest=SimpleNamespace(
            image_filepath_list=[str(existing), str(tmp_path / "missing.jpg")]
        )
    )
    assert _manifest_image_paths(result) == [existing]
    assert _manifest_image_paths(SimpleNamespace()) == []
