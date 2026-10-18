"""章节缓存与 LRU 淘汰测试。"""

from __future__ import annotations

from pathlib import Path

from astrbot_jmdownloader.core.cache import CachedChapter, ChapterCache


def _write_chapter(cache: ChapterCache, album_id: str, pid: str) -> Path:
    directory = cache.chapters_dir(album_id)
    path = directory / f"{pid}.pdf"
    path.write_bytes(b"%PDF-1.4 chapter " + pid.encode())
    return path


def _record(pid: str, pages: int = 3) -> CachedChapter:
    return CachedChapter(
        pid=pid,
        index="1",
        title=f"chapter-{pid}",
        page_count=pages,
        size_bytes=0,
    )


def test_cache_roundtrip(tmp_path):
    cache = ChapterCache(tmp_path / "cache")
    _write_chapter(cache, "111", "p1")
    cache.save_album("111", "title-111", [_record("p1", pages=5)])

    loaded = cache.load_chapters("111")
    assert set(loaded) == {"p1"}
    assert loaded["p1"].page_count == 5
    assert loaded["p1"].size_bytes > 0
    assert loaded["p1"].title == "chapter-p1"

    # 新实例从磁盘索引恢复
    reopened = ChapterCache(tmp_path / "cache")
    assert set(reopened.load_chapters("111")) == {"p1"}


def test_cache_skips_missing_pdf(tmp_path):
    cache = ChapterCache(tmp_path / "cache")
    path = _write_chapter(cache, "222", "p1")
    cache.save_album("222", "title-222", [_record("p1"), _record("p2")])
    path.unlink()
    loaded = cache.load_chapters("222")
    assert loaded == {}


def test_cache_lru_eviction(tmp_path):
    cache = ChapterCache(tmp_path / "cache")
    for album_id in ("111", "222", "333"):
        _write_chapter(cache, album_id, "p1")
        cache.save_album(album_id, f"title-{album_id}", [_record("p1")])

    # 让 111 成为最近访问
    cache.touch("111")
    removed = cache.evict(2)
    assert removed == ["222"]
    assert set(cache.load_chapters("111")) == {"p1"}
    assert set(cache.load_chapters("333")) == {"p1"}
    assert not cache.album_dir("222").is_dir()


def test_cache_evict_all_when_zero(tmp_path):
    cache = ChapterCache(tmp_path / "cache")
    for album_id in ("111", "222"):
        _write_chapter(cache, album_id, "p1")
        cache.save_album(album_id, "title", [_record("p1")])

    removed = sorted(cache.evict(0))
    assert removed == ["111", "222"]
    assert cache.album_titles() == {}


def test_cache_ignores_broken_index(tmp_path):
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    (root / "index.json").write_text("{not-json", encoding="utf-8")
    cache = ChapterCache(root)
    assert cache.load_chapters("111") == {}
