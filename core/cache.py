"""章节 PDF 缓存与 LRU 淘汰。

每本子一个独立目录，元数据跟着目录走：

    <root>/
    ├── <album_id>/
    │   ├── album.json          # 标题、最近访问时间、章节元数据
    │   └── chapters/<pid>.pdf  # 单章 PDF，可复用于整本/分卷
    └── ...

LRU 顺序不依赖任何全局索引：每次需要时扫描各本子目录，读取各自的
``album.json`` 里的最近访问时间即可。写入采用临时文件 + 原子替换，
进程中断不会留下半截 JSON；损坏或旧格式的元数据按“没有缓存”处理。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from astrbot.api import logger

ALBUM_META_NAME = "album.json"
ALBUM_META_VERSION = 2
CHAPTERS_DIRNAME = "chapters"


@dataclass(frozen=True)
class CachedChapter:
    """缓存索引中的章节记录。"""

    pid: str
    index: str
    title: str
    page_count: int
    size_bytes: int


def _atomic_json_write(target: Path, payload: Mapping[str, Any]) -> None:
    """把 JSON 先写临时文件再替换，避免半截元数据。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    staging = target.with_name(target.name + ".writing")
    staging.write_text(text, encoding="utf-8")
    staging.replace(target)


def _safe_count(raw: Any) -> int:
    """页数等计数值的宽容转换，坏值按 0 处理。"""
    try:
        return int(raw or 0)
    except (ValueError, TypeError):
        return 0


def _safe_timestamp(raw: Any) -> float:
    """访问时间等时间戳的宽容转换，坏值按 0 处理。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return float(raw)


def resolve_cache_root(plugin_name: str) -> Path | None:
    """返回插件数据目录下的 ``cache`` 根；不可用时返回 None（关闭缓存）。"""
    data_dir: Path | None = None
    try:
        from astrbot.core.star.star_tools import StarTools

        data_dir = Path(StarTools.get_data_dir(plugin_name))
    except Exception:  # noqa: BLE001 - 兼容不同 AstrBot 版本/测试环境
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        except Exception:  # noqa: BLE001
            data_dir = None

    if data_dir is None:
        logger.warning("无法获取插件数据目录，章节缓存已禁用", exc_info=True)
        return None

    cache_root = data_dir / "cache"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(
            "创建缓存目录失败，章节缓存已禁用：%s", cache_root, exc_info=True
        )
        return None
    return cache_root


class ChapterCache:
    """按本子缓存章节 PDF；每本子的元数据独立存放，LRU 由目录扫描得出。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 路径 -------------------------------------------------------------

    def album_dir(self, album_id: str) -> Path:
        return self.root / str(album_id)

    def chapters_dir(self, album_id: str) -> Path:
        target = self.album_dir(album_id) / CHAPTERS_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        return target

    def chapter_pdf_path(self, album_id: str, pid: str) -> Path:
        return self.chapters_dir(album_id) / f"{pid}.pdf"

    def _meta_path(self, album_id: str) -> Path:
        return self.album_dir(album_id) / ALBUM_META_NAME

    # -- 元数据读写 -------------------------------------------------------

    def _read_meta(self, album_id: str) -> dict[str, Any] | None:
        try:
            raw = self._meta_path(album_id).read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (ValueError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_meta(self, album_id: str, payload: Mapping[str, Any]) -> None:
        target = self._meta_path(album_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(target, payload)

    def _album_ids(self) -> list[str]:
        """扫描缓存根，返回所有本子目录名（排序保证结果稳定）。"""
        try:
            children = list(self.root.iterdir())
        except OSError:
            return []
        return sorted(child.name for child in children if child.is_dir())

    # -- 查询 -------------------------------------------------------------

    def load_chapters(self, album_id: str) -> dict[str, CachedChapter]:
        """返回缓存目录里仍有 PDF 的章节；缺失或损坏的条目直接跳过。"""
        album_ref = str(album_id)
        meta = self._read_meta(album_ref)
        if meta is None:
            return {}
        recorded = meta.get("chapters")
        if not isinstance(recorded, list):
            return {}

        pdf_dir = self.album_dir(album_ref) / CHAPTERS_DIRNAME
        chapters: dict[str, CachedChapter] = {}
        for entry in recorded:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("pid") or "")
            if pid == "":
                continue
            try:
                size_bytes = (pdf_dir / f"{pid}.pdf").stat().st_size
            except OSError:
                continue
            if size_bytes <= 0:
                continue
            chapters[pid] = CachedChapter(
                pid=pid,
                index=str(entry.get("index") or ""),
                title=str(entry.get("title") or ""),
                page_count=_safe_count(entry.get("page_count")),
                size_bytes=size_bytes,
            )
        return chapters

    # -- 更新 -------------------------------------------------------------

    def save_album(
        self, album_id: str, album_title: str, chapters: Iterable[CachedChapter]
    ) -> None:
        """写入一本子的章节元数据，同时刷新最近访问时间。"""
        album_ref = str(album_id)
        stamp = time.time()
        previous = self._read_meta(album_ref) or {}
        registered = previous.get("created_at")
        if _safe_timestamp(registered) <= 0:
            registered = stamp
        entries = [
            {
                "pid": str(record.pid),
                "index": str(record.index),
                "title": str(record.title),
                "page_count": _safe_count(record.page_count),
            }
            for record in chapters
        ]
        self._write_meta(
            album_ref,
            {
                "version": ALBUM_META_VERSION,
                "title": str(album_title),
                "created_at": _safe_timestamp(registered),
                "last_access": stamp,
                "chapters": entries,
            },
        )

    def touch(self, album_id: str) -> None:
        """刷新最近访问时间；没有元数据的本子静默忽略。"""
        album_ref = str(album_id)
        meta = self._read_meta(album_ref)
        if meta is None:
            return
        meta["last_access"] = time.time()
        self._write_meta(album_ref, meta)

    # -- 淘汰 -------------------------------------------------------------

    def evict(self, max_albums: int) -> list[str]:
        """按最近访问时间保留 ``max_albums`` 本，返回被删除的本子 ID。"""
        ranking: list[tuple[float, str]] = []
        for album_id in self._album_ids():
            meta = self._read_meta(album_id) or {}
            ranking.append((_safe_timestamp(meta.get("last_access")), album_id))

        # 最近访问时间降序；时间相同按目录名升序，保证淘汰结果可预测
        ranking.sort(key=lambda item: (-item[0], item[1]))
        if max_albums < 1:
            doomed = [album_id for _, album_id in ranking]
        else:
            doomed = [album_id for _, album_id in ranking[max_albums:]]

        for album_id in doomed:
            target = self.album_dir(album_id)
            shutil.rmtree(target, ignore_errors=True)
        return doomed

    def album_titles(self) -> dict[str, str]:
        """返回缓存中的本子标题（排障/日志用）。"""
        titles: dict[str, str] = {}
        for album_id in self._album_ids():
            meta = self._read_meta(album_id)
            if meta is not None:
                titles[album_id] = str(meta.get("title") or "")
        return titles
