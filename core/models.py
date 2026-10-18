"""插件内部使用的领域数据模型（与 AstrBot / jmcomic 解耦）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

MIB = 1024 * 1024


class DeliveryMode(str, Enum):
    """PDF 交给协议端的方式，决定分卷规划的硬上限。"""

    FILE_URL = "file_url"
    BASE64 = "base64"


@dataclass(frozen=True)
class ChapterRef:
    """相册里的一个章节（相册元数据，尚未下载）。"""

    pid: str
    index: str
    title: str


@dataclass(frozen=True)
class ChapterQuery:
    """章节筛选参数（--head/--tail/--latest/--range）。"""

    kind: str
    """head / tail / latest / range。"""

    number: int
    """head/tail/latest 为章数；range 为起始章节号。"""

    count: int | None = None
    """range 的章数：正整数或 -1（直到最新）；其它类型为 None。"""


@dataclass(frozen=True)
class CommandRequest:
    """``/jm`` 命令解析结果。"""

    album_id: str
    chapter_no: int | None = None
    selector: ChapterQuery | None = None
    no_cache: bool = False
    file_type: str | None = None
    """显式 --type；None 表示使用配置默认格式。"""
    preview: int | None = None
    """预览张数：None=非预览；0=使用配置默认值；正整数=指定张数。"""


@dataclass(frozen=True)
class AlbumInfo:
    """一次 /jm 任务的相册元数据。"""

    album_id: str
    title: str
    chapters: tuple[ChapterRef, ...]

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)


@dataclass(frozen=True)
class ChapterPdf:
    """已经落盘的章节 PDF。"""

    pid: str
    index: str
    title: str
    page_count: int
    size_bytes: int
    path: Path


@dataclass(frozen=True)
class ChapterArtifact:
    """章节下载与合成结果（尚未附加相册元数据）。"""

    pid: str
    page_count: int
    size_bytes: int
    image_bytes: int
    pdf_path: Path


@dataclass
class JobStats:
    """一次任务的下载统计（用于消息、日志与分卷规划）。"""

    reused_chapters: int = 0
    downloaded_chapters: int = 0
    downloaded_image_bytes: int = 0
    failed_pids: list[str] = field(default_factory=list)

    @property
    def total_image_mib(self) -> float:
        return self.downloaded_image_bytes / MIB


@dataclass(frozen=True)
class VolumePlan:
    """规划器给出的单卷结果。"""

    number: int
    chapters: tuple[ChapterPdf, ...]
    estimated_bytes: int

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)


@dataclass(frozen=True)
class SplitPlan:
    """规划器给出的完整分卷方案。"""

    volumes: tuple[VolumePlan, ...]
    target_bytes: int
    soft_limit_bytes: int
    hard_limit_bytes: int
    mode: str
    exceeds_hard_limit: bool = False
    basis: str = ""
    """规划依据的可读描述（写入日志，便于事后解释为什么这样切）。"""

    @property
    def volume_count(self) -> int:
        return len(self.volumes)

    @property
    def target_mib(self) -> float:
        return self.target_bytes / MIB

    @property
    def hard_limit_mib(self) -> float:
        return self.hard_limit_bytes / MIB
