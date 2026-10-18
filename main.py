"""JM 下载插件的 AstrBot 入口。

模块分工：

* 本模块负责命令解析、任务编排、结果文案与文件投递；
* :mod:`core.config` 提供不可变配置快照与 jmcomic 选项；
* :mod:`core.jm_source` / :mod:`core.pdf` 负责相册抓取与 PDF 合成、加密；
* :mod:`core.planner` 按实测体积规划分卷回退方案；
* :mod:`core.cache` / :mod:`core.delivery` 负责章节缓存与跨容器上传。

一次 ``/jm`` 任务的执行顺序：读取相册元数据 → 复用缓存并补下载缺失章节 →
整本合成上传 → 上传失败时按章节边界分卷回退 → 上传成功后单独播报打开密码。
"""

# 入口层只依赖 AstrBot 的少量公共接口，其余能力都放在 core/ 包内。

from __future__ import annotations

import asyncio
import base64
import io
import re
from asyncio import Lock, Semaphore, gather, sleep, to_thread
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from secrets import choice
from shutil import rmtree
from tempfile import gettempdir, mkdtemp
from time import time

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig

try:  # AstrBot 内置命令的写法：用一个 GreedyStr 参数吃下命令后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - 单测环境/旧版本没有该类型
    GreedyStr = str  # type: ignore[assignment,misc]

from .core.cache import CachedChapter, ChapterCache, resolve_cache_root
from .core.archive import (
    OUTPUT_TYPE_HINT,
    archive_suffix,
    build_archive,
    normalize_archive_format,
)
from .core.config import (
    DEFAULT_PLUGIN_NAME,
    PluginConfig,
    parse_default_file_type,
)
from .core.delivery import resolve_callback_base, upload_pdf
from .core.errors import (
    ArchiveBuildError,
    ChapterOutOfRangeError,
    JmDownloaderError,
    PartialChapterError,
    PdfBuildError,
    SplitNotPossibleError,
    UploadConfigError,
)
from .core.jm_source import JmSource
from .core.models import (
    AlbumInfo,
    ChapterPdf,
    ChapterQuery,
    ChapterRef,
    CommandRequest,
    DeliveryMode,
    SubscriptionRequest,
    JobStats,
    MIB,
    SplitPlan,
    VolumePlan,
)
from .core.pdf import encrypt_pdf, merge_pdfs
from .core.planner import SplitInput, plan_split
from .core.subscriptions import (
    DEFAULT_NOTICE_CRON,
    DEFAULT_NOTICE_TIMING,
    DEFAULT_NOTICE_TYPE,
    DEFAULT_PUSH_MODE,
    DEFAULT_SCHEDULE_KIND,
    DEFAULT_SCHEDULE_VALUE,
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    build_target,
    next_run,
    now_text,
    parse_loop_interval,
    resolve_subscription_root,
    validate_cron,
)

# 每次任务的临时目录统一放在系统临时目录下，启动时顺手清理超时残留。
JOB_TEMP_ROOT = Path(gettempdir()) / "astrbot_jmdownloader"

# JM 相册 ID 是 3~12 位数字，命令里允许带 jm 前缀。
ALBUM_ID_RE = re.compile(r"(?:[Jj][Mm])?\s*([0-9]{3,12})")
_INT_TOKEN_RE = re.compile(r"-?[0-9]+")

# 文件名中被替换成下划线的字符：全部 C0 控制字符与常见保留符号。
_UNSAFE_FILENAME_CODES = {code: "_" for code in range(0x20)}
_UNSAFE_FILENAME_CODES.update({ord(mark): "_" for mark in '\\/:*?"<>|'})

# 密码字符集去掉 0/O/1/l/I 等易混字符，方便用户照着消息手动输入。
PASSWORD_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
PDF_PASSWORD_LENGTH: int = 8

# 超过这个时长没有改动的临时任务目录视为上次进程的遗留。
STALE_JOB_SECONDS = 6 * 60 * 60

_NO_CACHE_TOKENS = frozenset({"--no-cache", "--nocache", "--no_cache", "nocache"})
_INFO_TOKENS = frozenset({"--info", "-i"})
_SELECTOR_TOKENS = frozenset({"--head", "--tail", "--latest", "--range"})
_HELP_TOKENS = frozenset({"help", "-h", "--help", "?", "？"})

HELP_TEXT = """JM PDF 下载器命令说明

/jm <漫画ID>
    整本下载并合成一个 PDF 发送；上传失败时按章节边界自动分卷重发。

/jm <漫画ID> <章节号>
    只处理指定章节并发送单章 PDF，例如 /jm 123456 3、/jm 123456 第3章。

/jm <漫画ID> --head [N]
    取开头 N 章（省略 N 时为 1 章），合并成一个 PDF 发送。

/jm <漫画ID> --tail [N]
    取末尾 N 章（省略 N 时为 1 章），合并成一个 PDF 发送。

/jm <漫画ID> --latest [N]
    取最新 N 章（省略 N 时为 1 章）；相册章节按 index 升序，与 --tail 同义。

/jm <漫画ID> --range <起始章> [章数]
    从起始章连续取若干章；章数省略时为 1，写成 -1 表示一直取到最新。

/jm <漫画ID> --no-cache
    本次忽略已有章节缓存、强制重新拉取；结束后用新结果刷新缓存。

/jm <漫画ID> --type <pdf|zip|7z|tar|tar.gz|tar.bz2|tar.xz>
    选择发送格式：pdf（默认）为合并后的单个 PDF；
    zip/7z/tar 系列把各章 PDF 打包成压缩包发送。
    未写 --type 时使用配置中的「默认发送格式」。

/jm <漫画ID> --pre [N]
    预览模式：取所选范围（整本/单章/一条范围）第一章的前 N 页，
    按 --type 或配置 default_file_type 生成 PDF/压缩包发送，密码按配置；
    N 省略或写 0 时使用配置 preview_pages（默认 10）。

/jm <漫画ID> --info
    查询本子详情（标题、作者、标签、页数/章节、日期、简介等），
    并把封面与详情排版到同一张图片发送；渲染失败回退文字+封面；不下载章节。

/jm <漫画ID> --sub <loop 间隔|cron 表达式>
    订阅本子更新：按 loop（如 30m/6h/1d）或 cron（5 段表达式）检查；
    可配 --type 指定推送格式，--push file|notice 选择推送文件或仅通知；
    --notice <group|user> [会话ID,...] <realtime|cron> [cron表达式] 指定目标与时机。
    /jm <漫画ID> --sub off 取消订阅。订阅也可在插件 WebUI「订阅管理」页维护。

规则：
1. -- 开头的开关可以放在 /jm 后的任意位置，例如 /jm --no-cache 123456。
2. 章节号与 --head/--tail/--latest/--range 不能同时使用，筛选开关之间也互斥。
3. --no-cache 可以和任意章节筛选组合，例如 /jm 123456 --head 3 --no-cache。
4. --type 可以放在任意位置，例如 /jm 123456 --head 3 --type zip。
5. --pre 可与 --type、--head/--tail/--latest/--range 组合，顺序不限；预览文件按所选/默认格式生成。
6. PDF/压缩包默认使用随机密码加密，上传完成后单独发送密码；固定密码在插件配置中填写。"""

GROUP_BLOCK_MESSAGES = {
    "disabled": "当前配置下 /jm 只在私聊生效。",
    "blacklist": "当前群聊已被加入黑名单，不能使用 /jm。",
    "whitelist": "当前群聊不在白名单内，不能使用 /jm。",
}


def group_block_message(event: AstrMessageEvent, config: PluginConfig) -> str | None:
    """群聊被禁用/拉黑/未进白名单时返回提示文案。"""
    group_id = str(event.get_group_id() or "").strip()
    if not group_id:
        return None
    return GROUP_BLOCK_MESSAGES.get(config.group_access(group_id))


USAGE_HINT = (
    "用法：/jm <数字ID> [章节号] "
    "[--head/--tail/--latest/--range ... | --no-cache | "
    "--type <pdf|zip|7z|tar|tar.gz|tar.bz2|tar.xz> | --pre [N] | "
    "--sub <loop 间隔|cron 表达式> | --sub off | --info]；"
    "发送 /jm help 查看完整说明"
)


def is_help_request(raw: str) -> bool:
    """无参数或参数中出现 help/-h/--help 都按帮助请求处理。"""
    words = str(raw or "").split()
    if not words:
        return True
    return any(word.lower() in _HELP_TOKENS for word in words)


def _filename_fragment(raw: str, limit: int) -> str:
    """把标题等文本压成可以安全放进文件名的片段。"""
    text = str(raw or "").translate(_UNSAFE_FILENAME_CODES)
    return " ".join(text.split()).strip("._ ")[:limit]


def _pdf_stem(*parts: str) -> str:
    """把非空片段用 ``-`` 连起来，截断到 90 字符并补 ``.pdf``。"""
    return "-".join(part for part in parts if part)[:90] + ".pdf"


def format_mib(size_bytes: float) -> str:
    """把字节数格式化成便于阅读的 MiB 文本。"""
    return f"{size_bytes / MIB:.2f} MiB"


def normalize_album_id(raw: str) -> str | None:
    """从 ``123456`` / ``JM123456`` / ``jm 123456`` 中提取数字 ID。"""
    found = ALBUM_ID_RE.fullmatch(str(raw or "").strip())
    return found.group(1) if found else None


def generate_pdf_password(length: int = PDF_PASSWORD_LENGTH) -> str:
    """从去混淆字符集中随机取 ``length`` 个字符组成打开密码。"""
    characters = [choice(PASSWORD_ALPHABET) for _ in range(length)]
    return "".join(characters)


def album_pdf_name(album_id: str, album_title: str) -> str:
    """整本单文件的上传文件名。"""
    return _pdf_stem(f"JM{album_id}", _filename_fragment(album_title, 40))


def volume_pdf_name(
    album_id: str, album_title: str, number: int, total: int
) -> str:
    """回退分卷的上传文件名；只有一卷时不带卷号。"""
    pieces = [f"JM{album_id}"]
    if total > 1:
        pieces += [f"卷{number}of{total}"]
    return _pdf_stem(*pieces, _filename_fragment(album_title, 40))


def chapter_pdf_name(
    album_id: str,
    album_title: str,
    chapter_no: int,
    total: int,
    chapter_title: str = "",
) -> str:
    """单章模式的上传文件名；章节标题为空时回退到相册标题。"""
    caption = _filename_fragment(chapter_title or album_title, 40)
    return _pdf_stem(f"JM{album_id}", f"章{chapter_no}of{total}", caption)


def album_ref(album: AlbumInfo) -> str:
    """相册的展示引用。"""
    return f"JM{album.album_id}"


def preview_pdf_name(
    album_id: str,
    album_title: str,
    chapter_no: int,
    total: int,
    chapter_title: str,
    pages: int,
) -> str:
    """预览文件的上传文件名：标注章节与预览页数。"""
    caption = _filename_fragment(chapter_title or album_title, 30)
    return _pdf_stem(
        f"JM{album_id}",
        f"章{chapter_no}of{total}",
        f"预览{pages}页",
        caption,
    )


_CHAPTER_NO_RE = re.compile(
    r"^(?:ch(?:apter)?[\s._-]*)?(?:第\s*)?([0-9]+)(?:\s*[章话話])?$",
    re.IGNORECASE,
)


def normalize_chapter_no(raw: str) -> int | None:
    """解析章节号；空字符串表示整本模式，非法写法抛 ``ValueError``。

    接受 ``3`` / ``第3章`` / ``ch3`` / ``chapter 3`` 等写法，返回从 1 开始的序号。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    matched = _CHAPTER_NO_RE.fullmatch(text)
    if matched is None:
        raise ValueError(f"无法解析章节号：{raw}")
    number = int(matched.group(1))
    if number < 1:
        raise ValueError(f"章节号必须从 1 开始：{raw}")
    return number


def _read_integer(
    words: list[str], cursor: int, label: str, *, fallback: int | None
) -> tuple[int | None, int]:
    """读取游标处的整数 token；``fallback`` 为 None 表示该参数必填。"""
    token = words[cursor] if cursor < len(words) else ""
    if _INT_TOKEN_RE.fullmatch(token) is None:
        if fallback is None:
            raise ValueError(f"{label}需要一个数字")
        return fallback, cursor
    value = int(token)
    if value < 1:
        raise ValueError(f"{label}必须是正整数")
    return value, cursor + 1


def _parse_selector(
    flag: str, words: list[str], cursor: int
) -> tuple[ChapterQuery, int]:
    """解析章节筛选开关，返回 (筛选条件, 新的待读位置)。"""
    if flag != "--range":
        amount, cursor = _read_integer(words, cursor, f"{flag} 的章数", fallback=1)
        return ChapterQuery(flag[2:], amount), cursor
    start, cursor = _read_integer(
        words, cursor, "--range 的起始章节", fallback=None
    )
    count = 1
    if cursor < len(words) and _INT_TOKEN_RE.fullmatch(words[cursor]):
        count = int(words[cursor])
        cursor += 1
        if count != -1 and count < 1:
            raise ValueError("--range 的章数必须为正整数或 -1（表示到最新）")
    return ChapterQuery("range", start, count), cursor


def _take_cron_expression(
    words: list[str], cursor: int, label: str
) -> tuple[str, int]:
    """从 token 流中取 5 段 cron（兼容整段带引号的写法）。"""
    tokens = words[cursor : cursor + 5]
    if len(tokens) < 5 or any(token.startswith("--") for token in tokens):
        raise ValueError(f"{label} 需要 5 段 cron 表达式（分 时 日 月 周）")
    expression = " ".join(tokens).strip().strip("\"'")
    try:
        return validate_cron(expression), cursor + 5
    except SubscriptionError as exc:
        raise ValueError(str(exc)) from exc


def _parse_subscription_flags(
    word: str, words: list[str], cursor: int, state: dict
) -> int | None:
    """处理 --sub / --notice / --push；返回新的游标（None 表示未处理）。"""
    flag = word.lower()
    if flag == "--sub":
        state["seen"] = True
        if cursor < len(words) and words[cursor].lower() == "off":
            state["schedule_kind"] = "off"
            return cursor + 1
        if cursor < len(words) and words[cursor].lower() in {"cron", "loop"}:
            state["schedule_kind"] = words[cursor].lower()
            cursor += 1
            if state["schedule_kind"] == "cron":
                expression, cursor = _take_cron_expression(
                    words, cursor, "--sub cron"
                )
                state["schedule_value"] = expression
            elif cursor < len(words) and not words[cursor].startswith("--"):
                value = words[cursor]
                cursor += 1
                try:
                    parse_loop_interval(value)
                except SubscriptionError as exc:
                    raise ValueError(str(exc)) from exc
                state["schedule_value"] = value
        return cursor
    if flag == "--push":
        state["seen"] = True
        if cursor >= len(words):
            raise ValueError("--push 需要 file 或 notice")
        mode = words[cursor].strip().lower()
        if mode not in {"file", "notice"}:
            raise ValueError("--push 只能是 file 或 notice")
        state["push_mode"] = mode
        return cursor + 1
    if flag == "--notice":
        state["seen"] = True
        if cursor >= len(words) or words[cursor].startswith("--"):
            return cursor
        notice_type = words[cursor].strip().lower()
        if notice_type in {"group", "user"}:
            state["notice_type"] = notice_type
            cursor += 1
            if (
                cursor < len(words)
                and not words[cursor].startswith("--")
                and words[cursor].lower() not in {"realtime", "cron"}
            ):
                state["notice_sids"] = words[cursor]
                cursor += 1
        timing = (
            words[cursor].strip().lower() if cursor < len(words) else ""
        )
        if timing in {"realtime", "cron"}:
            state["notice_timing"] = timing
            cursor += 1
            if timing == "cron":
                expression, cursor = _take_cron_expression(
                    words, cursor, "--notice cron"
                )
                state["notice_cron"] = expression
        return cursor
    return None


def parse_command_arguments(raw: str) -> CommandRequest:
    """解析 ``/jm`` 后的全部参数。

    ``--`` 开头的开关可以出现在任意位置；位置参数依次是漫画 ID 与可选章节号。
    章节号与章节筛选开关互斥，筛选开关彼此也互斥。
    """
    words = str(raw or "").split()
    positionals: list[str] = []
    selector: ChapterQuery | None = None
    no_cache = False
    file_type: str | None = None
    preview: int | None = None
    sub_state: dict = {"seen": False}
    info = False
    cursor = 0
    while cursor < len(words):
        word = words[cursor]
        cursor += 1
        if not word.startswith("--"):
            positionals.append(word)
            continue
        flag = word.lower()
        if flag in _INFO_TOKENS:
            info = True
            continue
        handled = _parse_subscription_flags(word, words, cursor, sub_state)
        if handled is not None:
            cursor = handled
            continue
        if flag in _NO_CACHE_TOKENS:
            no_cache = True
            continue
        if flag == "--type" or flag.startswith("--type="):
            if "=" in word:
                value = word.split("=", 1)[1]
            elif cursor < len(words):
                value = words[cursor]
                cursor += 1
            else:
                raise ValueError("--type 需要一个文件格式值")
            try:
                file_type = (
                    "pdf"
                    if value.strip().lower() == "pdf"
                    else normalize_archive_format(value)
                )
            except ArchiveBuildError as exc:
                raise ValueError(exc.user_message) from exc
            continue
        if flag == "--pre" or flag.startswith("--pre="):
            if "=" in word:
                raw_value = word.split("=", 1)[1].strip()
                if not raw_value.isdigit():
                    raise ValueError("--pre 的张数必须是整数")
                preview = int(raw_value)
            else:
                preview = 0
                takes_value = False
                if cursor < len(words) and words[cursor].isdigit():
                    if positionals or cursor + 1 < len(words):
                        takes_value = True
                if takes_value:
                    preview = int(words[cursor])
                    cursor += 1
                if preview < 0 or preview > 50:
                    raise ValueError("--pre 的张数需在 0-50 之间")
            continue
        if flag not in _SELECTOR_TOKENS:
            raise ValueError(
                f"未知参数 {word}；可用开关："
                "--head/--tail/--latest/--range/--no-cache/--type/--pre"
            )
        if selector is not None:
            raise ValueError("--head/--tail/--latest/--range 只能使用一个")
        selector, cursor = _parse_selector(flag, words, cursor)
    if not positionals:
        raise ValueError("缺少漫画 ID")
    if len(positionals) > 2:
        raise ValueError("位置参数过多：只接受 漫画ID [章节号]")
    chapter_no = (
        normalize_chapter_no(positionals[1]) if len(positionals) == 2 else None
    )
    if chapter_no is not None and selector is not None:
        raise ValueError("章节号与 --head/--tail/--latest/--range 不能同时使用")
    subscription = None
    if sub_state["seen"]:
        subscription = SubscriptionRequest(
            schedule_kind=sub_state.get("schedule_kind"),
            schedule_value=sub_state.get("schedule_value", ""),
            file_type=file_type,
            push_mode=sub_state.get("push_mode"),
            notice_type=sub_state.get("notice_type"),
            notice_sids=sub_state.get("notice_sids", ""),
            notice_timing=sub_state.get("notice_timing"),
            notice_cron=sub_state.get("notice_cron", ""),
        )
        file_type = None
    return CommandRequest(
        positionals[0],
        chapter_no,
        selector,
        no_cache,
        file_type,
        preview,
        subscription,
        info,
    )


def select_chapter(album: AlbumInfo, chapter_no: int) -> ChapterRef:
    """按章节号取一章：优先匹配章节自身 index，否则按相册顺序取第 N 章。"""
    for chapter in album.chapters:
        marker = chapter.index.strip()
        if marker.isdigit() and int(marker) == chapter_no:
            return chapter
    total = album.chapter_count
    if not 1 <= chapter_no <= total:
        raise ChapterOutOfRangeError(
            f"JM{album.album_id} 共 {total} 章，章节号应在 1-{total} 之间"
            f"（你填的是 {chapter_no}）。"
        )
    return album.chapters[chapter_no - 1]


_SCOPE_PREFIX = {"head": "前", "tail": "末尾", "latest": "最新"}


def resolve_selected_chapters(
    album: AlbumInfo, query: ChapterQuery
) -> tuple[ChapterRef, ...]:
    """把 --head/--tail/--latest/--range 解析成按相册顺序排列的章节列表。"""
    total = album.chapter_count
    kind = query.kind
    if kind in _SCOPE_PREFIX:
        amount = min(total, max(1, query.number))
        if kind == "head":
            return album.chapters[:amount]
        # JM 相册章节按 index 升序排列，所以末尾即最新。
        return album.chapters[total - amount :]
    if kind == "range":
        start = query.number
        if start < 1 or start > total:
            raise ChapterOutOfRangeError(
                f"JM{album.album_id} 共 {total} 章，--range 起始章节应在 "
                f"1-{total} 之间（你填的是 {start}）。"
            )
        count = 1 if query.count is None else query.count
        room = total - start + 1
        span = room if count == -1 else min(count, room)
        return album.chapters[start - 1 : start - 1 + span]
    raise ChapterOutOfRangeError(f"未知的章节筛选参数：{kind}")


def selection_label(
    query: ChapterQuery, selected_count: int, total: int
) -> str:
    """章节筛选范围的展示文案。"""
    prefix = _SCOPE_PREFIX.get(query.kind)
    if prefix is not None:
        return f"{prefix} {selected_count} 章（共 {total} 章）"
    start = query.number
    if query.count == -1:
        return f"第 {start} 章至最新（{selected_count} 章，共 {total} 章）"
    if selected_count <= 1:
        return f"第 {start} 章（共 {total} 章）"
    return f"第 {start}-{start + selected_count - 1} 章（共 {total} 章）"


def _selection_tag(query: ChapterQuery, selected_count: int) -> str:
    """章节筛选范围在文件名里的短标签。"""
    prefix = _SCOPE_PREFIX.get(query.kind)
    if prefix is not None:
        return f"{prefix}{selected_count}章"
    start = query.number
    if query.count == -1:
        return f"第{start}章起"
    if selected_count <= 1:
        return f"第{start}章"
    return f"第{start}-{start + selected_count - 1}章"


def selection_pdf_name(
    album_id: str,
    album_title: str,
    query: ChapterQuery,
    selected_count: int,
    album_total: int,
) -> str:
    """多章筛选模式的上传文件名。"""
    return _pdf_stem(
        f"JM{album_id}",
        _selection_tag(query, selected_count),
        _filename_fragment(album_title, 40),
    )


def chapter_position(album: AlbumInfo, pid: str) -> int:
    """章节在相册中的 1 起顺序位置；找不到时按 1 处理。"""
    for order, chapter in enumerate(album.chapters):
        if chapter.pid == pid:
            return order + 1
    return 1


def resolve_pdf_password(config: PluginConfig) -> str:
    """本次 PDF 的打开密码：固定密码优先，否则随机生成。"""
    if not config.encrypt_pdf:
        return ""
    fixed = str(config.pdf_password or "").strip()
    return fixed or generate_pdf_password()


@dataclass(frozen=True)
class _Scope:
    """命令模式解析结果：要处理的章节、展示文案与开头进度文案。"""

    chapters: tuple[ChapterRef, ...] | None
    label: str
    intro: str


@dataclass
class _JobContext:
    """一次任务中各个投递分支共享的状态。"""

    event: AstrMessageEvent
    album: AlbumInfo
    config: PluginConfig
    job_root: Path
    chapters: list[ChapterPdf]
    stats: JobStats
    password: str
    cache_note: str
    selector: ChapterQuery | None
    label: str

    @property
    def album_ref(self) -> str:
        return f"JM{self.album.album_id}"

    @property
    def encrypt_note(self) -> str:
        return "，已加密" if self.password else ""

    def report(self, headline: str, *, with_password: bool = True) -> str:
        """在结论行后追加缓存说明与打开密码。"""
        lines = [headline]
        if self.cache_note:
            lines.append(f"（{self.cache_note}）")
        if with_password and self.password:
            lines.append(f"打开密码：{self.password}")
        return "\n".join(lines)


class _PushUploadEvent:
    """把订阅推送目标包装成 delivery.upload_pdf 需要的事件形状。"""

    def __init__(self, umo: str, is_group: bool, session_id: str, bot: object):
        self.unified_msg_origin = umo
        self._is_group = is_group
        self._session_id = session_id
        self.bot = SimpleNamespace(
            api=SimpleNamespace(call_action=bot.call_action)
        )

    def get_group_id(self) -> str:
        return self._session_id if self._is_group else ""

    def get_sender_id(self) -> str:
        return self._session_id


class JmDownloaderPlugin(Star):
    """使用 ``/jm <ID>`` 下载 JM 漫画并以 PDF 形式发送。

    不带章节号时整本优先，上传失败后按章分卷回退；``/jm <ID> <章节号>`` 只
    处理指定章节；``--head/--tail/--latest/--range`` 按范围取章合并发送；
    ``--no-cache`` 强制忽略已有缓存重新拉取。
    """

    def __init__(
        self, context: Context, config: AstrBotConfig | dict | None = None
    ) -> None:
        super().__init__(context)
        self.raw_config = config or {}
        snapshot = PluginConfig.from_mapping(self.raw_config)
        self._semaphore = Semaphore(snapshot.max_concurrent)
        self._cache_lock = Lock()
        self._sub_task: asyncio.Task | None = None
        self._sub_store_cache: SubscriptionStore | None = None
        JOB_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_jobs()

    # ---- 生命周期 -----------------------------------------------------

    async def initialize(self) -> None:
        """插件激活：注册 Web API 并启动订阅轮询任务。"""
        self._register_web_apis()
        self._ensure_subscription_loop()

    async def terminate(self) -> None:
        """插件停用/重载：取消订阅轮询任务。"""
        task = self._sub_task
        self._sub_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _register_web_apis(self) -> None:
        """注册订阅管理页使用的后端 API。"""
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        base = f"/{DEFAULT_PLUGIN_NAME}"
        register(f"{base}/subscriptions", self.web_subscriptions, ["GET"], "订阅列表")
        register(
            f"{base}/subscriptions/save",
            self.web_subscription_save,
            ["POST"],
            "保存订阅",
        )
        register(
            f"{base}/subscriptions/delete",
            self.web_subscription_delete,
            ["POST"],
            "删除订阅",
        )
        register(
            f"{base}/settings/save",
            self.web_settings_save,
            ["POST"],
            "保存订阅默认值",
        )

    def _subscription_store(self) -> SubscriptionStore | None:
        if self._sub_store_cache is None:
            root = resolve_subscription_root(DEFAULT_PLUGIN_NAME)
            if root is None:
                return None
            self._sub_store_cache = SubscriptionStore(root)
        return self._sub_store_cache

    def _ensure_subscription_loop(self) -> None:
        if self._sub_task is None or self._sub_task.done():
            self._sub_task = asyncio.create_task(self._subscription_loop())

    async def _subscription_loop(self) -> None:
        """后台轮询订阅；异常只记录，不终止循环。"""
        while True:
            config = PluginConfig.from_mapping(self.raw_config)
            try:
                await self._check_subscriptions(config)
            except Exception:  # noqa: BLE001 - 后台任务不能因单次失败退出
                self.logger.exception("订阅轮询失败")
            await asyncio.sleep(config.sub_check_interval)

    def _subscription_defaults(self, config: PluginConfig) -> dict:
        """页面默认值优先于插件配置，最后回落到内置默认值。"""
        store = self._subscription_store()
        page_defaults = store.load_defaults() if store is not None else {}
        merged = {
            "schedule_kind": config.sub_default_schedule_kind
            or DEFAULT_SCHEDULE_KIND,
            "schedule_value": config.sub_default_schedule_value
            or DEFAULT_SCHEDULE_VALUE,
            "file_type": config.sub_default_type or "pdf",
            "push_mode": config.sub_default_push_mode or DEFAULT_PUSH_MODE,
            "notice_type": config.sub_default_notice_type
            or DEFAULT_NOTICE_TYPE,
            "notice_sids": config.sub_default_notice_sids,
            "notice_timing": config.sub_default_notice_timing
            or DEFAULT_NOTICE_TIMING,
            "notice_cron": config.sub_default_notice_cron
            or DEFAULT_NOTICE_CRON,
        }
        for key, value in page_defaults.items():
            if value not in (None, ""):
                merged[key] = value
        return merged

    # ---- 命令入口 -----------------------------------------------------

    @filter.command(
        "jm",
        desc="下载 JM 本子并发送 PDF/压缩包：/jm <数字ID> "
        "[章节号|--head/--tail/--latest/--range] "
        "[--no-cache] [--type pdf|zip|7z|tar|tar.gz|tar.bz2|tar.xz]",
    )
    async def download_command(
        self,
        event: AstrMessageEvent,
        args: GreedyStr,
    ) -> None:
        # 用协程 handler + event.send 发消息，而不是 yield 异步生成器：
        # AstrBot 管道在 event 已 stop 时会 break 迭代且不 aclose 生成器，
        # 里面的 finally/async with 只能等 GC 才执行；协程 handler 会完整跑到返回。
        event.stop_event()
        config = PluginConfig.from_mapping(self.raw_config)
        raw_args = str(args or "")
        if not config.enabled:
            await self._say(event, "该 JM 下载插件目前处于关闭状态。")
            return
        blocked = group_block_message(event, config)
        if blocked:
            await self._say(event, blocked)
            return
        if is_help_request(raw_args):
            await self._send_help(event)
            return
        reply = await self._run_command(event, config, raw_args)
        if reply:
            await self._say(event, reply)

    async def _say(self, event: AstrMessageEvent, text: str) -> None:
        """发送一条纯文本消息。"""
        await event.send(MessageChain().message(text))

    async def _run_command(
        self,
        event: AstrMessageEvent,
        config: PluginConfig,
        raw_args: str,
    ) -> str:
        """校验参数与权限，占用并发信号量后执行一次任务并返回结果文案。"""
        if not config.enabled:
            return "该 JM 下载插件目前处于关闭状态。"
        blocked = group_block_message(event, config)
        if blocked:
            return blocked
        if is_help_request(raw_args):
            return HELP_TEXT
        try:
            request = parse_command_arguments(raw_args)
        except ValueError as exc:
            return f"参数错误：{exc}。{USAGE_HINT}"
        album_id = normalize_album_id(request.album_id)
        if not album_id:
            return USAGE_HINT
        file_type = request.file_type or config.default_file_type
        if request.info:
            return await self._handle_info(event, config, album_id)
        if request.subscription is not None:
            async with self._semaphore:
                return await self._handle_subscription(
                    event, config, request
                )
        if event.get_group_id() and not config.allow_group:
            return "当前配置下 /jm 只在私聊生效。"

        if self._semaphore.locked():
            suffix = "（强制无缓存）" if request.no_cache else ""
            await self._say(
                event, f"JM{album_id}{suffix} 已加入下载队列，请稍候……"
            )

        async with self._semaphore:
            self._cleanup_stale_jobs()
            job_prefix = f"jm{album_id}-"
            job_root = Path(mkdtemp(prefix=job_prefix, dir=str(JOB_TEMP_ROOT)))
            try:
                return await self._run_job(
                    event,
                    album_id,
                    config,
                    job_root,
                    request.chapter_no,
                    request.no_cache,
                    request.selector,
                    file_type,
                    request.preview,
                )
            finally:
                rmtree(job_root, ignore_errors=True)

    # ---- 帮助 ---------------------------------------------------------

    async def _send_help(self, event: AstrMessageEvent) -> None:
        """优先把帮助渲染成图片发送，渲染或发送失败时回退纯文本。"""
        image = await self._render_help_image(event)
        if image:
            chain = MessageChain()
            if image.startswith("http"):
                chain.url_image(image)
            else:
                chain.file_image(image)
            try:
                await event.send(chain)
                return
            except Exception:  # noqa: BLE001 - 图片发送失败回退文本
                self.logger.warning("帮助图片未能发出，改为文字版", exc_info=True)
        await self._say(event, HELP_TEXT)

    async def _render_text_image(
        self, event: AstrMessageEvent, text: str
    ) -> str | None:
        """调用 AstrBot 的文本转图服务，失败返回 None。"""
        try:
            umo = getattr(event, "unified_msg_origin", None)
            rendered = await self.text_to_image(
                text, return_url=True, umo=umo
            )
        except Exception:  # noqa: BLE001 - t2i 不可用/未配置
            self.logger.warning("文本转图失败，改用文字版", exc_info=True)
            return None
        return rendered or None

    async def _send_rendered_text(
        self, event: AstrMessageEvent, text: str
    ) -> bool:
        """把文本渲染成图片发送；失败返回 False，由调用方回退文字。"""
        image = await self._render_text_image(event, text)
        if not image:
            return False
        chain = MessageChain()
        if image.startswith("http"):
            chain.url_image(image)
        else:
            chain.file_image(image)
        try:
            await event.send(chain)
        except Exception:  # noqa: BLE001 - 图片发送失败回退文字
            self.logger.warning("图片消息发送失败，改用文字版", exc_info=True)
            return False
        return True

    async def _render_help_image(
        self, event: AstrMessageEvent
    ) -> str | None:
        """调用 AstrBot 的文本转图服务渲染帮助，失败返回 None。"""
        return await self._render_text_image(event, HELP_TEXT)

    # ---- 详情 ---------------------------------------------------------

    INFO_CARD_TEMPLATE = """
<div class="jm-info-card">
  {% if cover %}<img class="jm-info-cover" src="{{ cover }}" alt="cover" />{% endif %}
  <div class="jm-info-body">
    <div class="jm-info-title">JM{{ album_id }}「{{ title }}」</div>
    {% for row in rows %}
    <div class="jm-info-row"><span class="k">{{ row[0] }}</span><span class="v">{{ row[1] }}</span></div>
    {% endfor %}
    {% if description %}<div class="jm-info-desc">{{ description }}</div>{% endif %}
    <div class="jm-info-url">{{ url }}</div>
  </div>
</div>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #f5f6f8; font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; }
  .jm-info-card { display: flex; gap: 18px; width: 760px; padding: 22px; background: #ffffff; color: #1f2328; }
  .jm-info-cover { width: 240px; max-height: 340px; object-fit: contain; border-radius: 8px; background: #eceff3; flex: none; }
  .jm-info-body { flex: 1; min-width: 0; }
  .jm-info-title { font-size: 21px; font-weight: 700; margin-bottom: 10px; word-break: break-all; }
  .jm-info-row { display: flex; gap: 8px; font-size: 13px; line-height: 1.9; }
  .jm-info-row .k { color: #6b7280; flex: none; width: 52px; }
  .jm-info-row .v { word-break: break-all; }
  .jm-info-desc { margin-top: 10px; font-size: 13px; line-height: 1.7; color: #374151; border-top: 1px solid #e5e7eb; padding-top: 9px; }
  .jm-info-url { margin-top: 10px; font-size: 12px; color: #2563eb; word-break: break-all; }
</style>
"""

    @staticmethod
    def _cover_data_uri(cover: Path) -> str:
        """把封面转成 JPEG data URI，便于一次性渲染进详情图。"""
        from PIL import Image

        with Image.open(cover) as image:
            rgb = image.convert("RGB")
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=88)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    async def _handle_info(
        self, event: AstrMessageEvent, config: PluginConfig, album_id: str
    ) -> str:
        """查询本子详情，把封面与信息排版到同一张图片发送。"""
        source = JmSource(config)
        try:
            album = await to_thread(source.fetch_album, album_id)
        except JmDownloaderError as exc:
            return f"JM{album_id} {exc.user_message}"
        cover_dir = Path(mkdtemp(prefix="jm-info-", dir=str(JOB_TEMP_ROOT)))
        cover_file: Path | None = None
        try:
            cover_file = await to_thread(
                source.fetch_album_cover,
                album.album_id,
                cover_dir / "cover.jpg",
            )
            rows: list[tuple[str, str]] = []
            if album.authors:
                rows.append(("作者", "、".join(album.authors)))
            if album.works:
                rows.append(("作品", "、".join(album.works)))
            if album.actors:
                rows.append(("角色", "、".join(album.actors)))
            if album.tags:
                rows.append(("标签", "、".join(album.tags)))
            stats = [f"章节 {album.chapter_count}"]
            if album.page_count:
                stats.append(f"页数 {album.page_count}")
            if album.comment_count:
                stats.append(f"评论 {album.comment_count}")
            rows.append(("统计", " · ".join(stats)))
            dates = []
            if album.publish_date:
                dates.append(f"发布 {album.publish_date}")
            if album.update_date:
                dates.append(f"更新 {album.update_date}")
            if dates:
                rows.append(("日期", " · ".join(dates)))
            if album.likes or album.views:
                rows.append(
                    (
                        "热度",
                        f"喜欢 {album.likes or '-'} · 观看 {album.views or '-'}",
                    )
                )
            description = " ".join(album.description.split())
            if len(description) > 200:
                description = description[:200] + "…"
            url = f"https://18comic.vip/album/{album.album_id}"
            if cover_file is not None:
                try:
                    cover_uri = await to_thread(
                        self._cover_data_uri, cover_file
                    )
                    rendered = await self.html_render(
                        self.INFO_CARD_TEMPLATE,
                        {
                            "cover": cover_uri,
                            "album_id": album.album_id,
                            "title": album.title,
                            "rows": rows,
                            "description": description,
                            "url": url,
                        },
                        return_url=True,
                        umo=getattr(event, "unified_msg_origin", None),
                    )
                    if rendered:
                        chain = MessageChain()
                        if rendered.startswith("http"):
                            chain.url_image(rendered)
                        else:
                            chain.file_image(rendered)
                        try:
                            await event.send(chain)
                            return ""
                        except Exception:  # noqa: BLE001 - 回退到文本+封面
                            self.logger.warning(
                                "详情图发送失败，回退文字", exc_info=True
                            )
                except Exception:  # noqa: BLE001 - 回退到文字+封面
                    self.logger.warning(
                        "详情图渲染失败，回退文字", exc_info=True
                    )
            # 回退：先发封面（如有），再发文字版详情
            if cover_file is not None:
                try:
                    await event.send(
                        MessageChain().file_image(str(cover_file))
                    )
                except Exception:  # noqa: BLE001 - 封面失败不影响详情
                    self.logger.warning("封面发送失败", exc_info=True)
            lines = [f"JM{album.album_id}「{album.title}」"]
            lines.extend(f"{key}：{value}" for key, value in rows)
            if description:
                lines.append("简介：" + description)
            lines.append(f"详情页：{url}")
            return "\n".join(lines)
        finally:
            rmtree(cover_dir, ignore_errors=True)

    # ---- 订阅 ---------------------------------------------------------

    async def _handle_subscription(
        self,
        event: AstrMessageEvent,
        config: PluginConfig,
        request: CommandRequest,
    ) -> str:
        """创建/更新/取消一个漫画订阅。"""
        store = self._subscription_store()
        if store is None:
            return "无法访问插件数据目录，订阅功能不可用。"
        sub_request = request.subscription
        assert sub_request is not None
        album_id = normalize_album_id(request.album_id)
        if not album_id:
            return USAGE_HINT
        if sub_request.schedule_kind == "off":
            removed = store.delete(album_id)
            return (
                f"已取消 JM{album_id} 的订阅。"
                if removed
                else f"JM{album_id} 当前没有订阅。"
            )
        defaults = self._subscription_defaults(config)
        kind = sub_request.schedule_kind or defaults["schedule_kind"]
        value = sub_request.schedule_value or defaults["schedule_value"]
        if kind == "loop":
            try:
                value = f"{parse_loop_interval(value)}s"
            except SubscriptionError as exc:
                return f"订阅参数错误：{exc}"
        elif kind == "cron":
            try:
                value = validate_cron(value)
            except SubscriptionError as exc:
                return f"订阅参数错误：{exc}"
        else:
            return "订阅方式只支持 loop 或 cron。"
        file_type = sub_request.file_type or defaults["file_type"]
        file_type = parse_default_file_type(file_type)
        push_mode = sub_request.push_mode or defaults["push_mode"]
        if push_mode not in {"file", "notice"}:
            return "推送内容只能是 file 或 notice。"
        notice_type = sub_request.notice_type or defaults["notice_type"]
        sids = sub_request.notice_sids or defaults["notice_sids"]
        targets = build_target(event, notice_type, sids)
        if not targets:
            return "订阅需要一个通知目标（群聊/私聊会话）。"
        timing = sub_request.notice_timing or defaults["notice_timing"]
        if timing not in {"realtime", "cron"}:
            return "通知时机只能是 realtime 或 cron。"
        notice_cron = sub_request.notice_cron or defaults["notice_cron"]
        try:
            validate_cron(notice_cron)
        except SubscriptionError as exc:
            return f"订阅参数错误：{exc}"
        source = JmSource(config)
        try:
            album = await to_thread(source.fetch_album, album_id)
        except JmDownloaderError as exc:
            return f"JM{album_id} {exc.user_message}"
        now = datetime.now()
        next_run_at = next_run(kind, value, now)
        next_notice_at = (
            next_run("cron", notice_cron, now) if timing == "cron" else ""
        )
        subscription = Subscription(
            album_id=album.album_id,
            album_title=album.title,
            schedule_kind=kind,
            schedule_value=value,
            file_type=file_type,
            push_mode=push_mode,
            notice_type=notice_type,
            targets=targets,
            notice_timing=timing,
            notice_cron=notice_cron,
            seen_pids=[chapter.pid for chapter in album.chapters],
            next_run_at=next_run_at.isoformat(timespec="seconds"),
            next_notice_at=(
                next_notice_at.isoformat(timespec="seconds")
                if next_notice_at
                else ""
            ),
            created_at=now_text(),
        )
        store.upsert(subscription)
        return (
            f"已订阅 JM{album.album_id}「{album.title}」："
            f"{kind} {value}，推送格式 {file_type}，"
            f"{'文件+通知' if push_mode == 'file' else '仅消息通知'}，"
            f"通知 {notice_type}（{len(targets)} 个会话），"
            f"更新检测后{'立即' if timing == 'realtime' else '按 ' + notice_cron}"
            f"推送；下次检查 {next_run_at.strftime('%Y-%m-%d %H:%M')}。"
        )

    async def _check_subscriptions(self, config: PluginConfig) -> None:
        """检查所有到期的订阅，发现新章节后按配置推送。"""
        store = self._subscription_store()
        if store is None:
            return
        subscriptions = store.load()
        if not subscriptions:
            return
        now = datetime.now()
        changed = False
        for subscription in subscriptions:
            if not subscription.enabled:
                continue
            due_run = subscription.next_run
            if due_run is not None and now < due_run:
                continue
            try:
                source = JmSource(config)
                album = await to_thread(source.fetch_album, subscription.album_id)
                current = [chapter.pid for chapter in album.chapters]
                seen = set(subscription.seen_pids)
                fresh = [pid for pid in current if pid not in seen]
                subscription.album_title = album.title or subscription.album_title
                subscription.seen_pids = current
                subscription.last_check_at = now_text()
                subscription.next_run_at = next_run(
                    subscription.schedule_kind,
                    subscription.schedule_value,
                    now,
                ).isoformat(timespec="seconds")
                if subscription.notice_timing == "cron":
                    if fresh:
                        subscription.pending_pids = list(
                            dict.fromkeys(subscription.pending_pids + fresh)
                        )
                        if not subscription.next_notice_at:
                            subscription.next_notice_at = next_run(
                                "cron", subscription.notice_cron, now
                            ).isoformat(timespec="seconds")
                elif fresh:
                    await self._push_subscription(
                        subscription, album, fresh, config
                    )
                due_notice = subscription.next_notice
                if (
                    subscription.pending_pids
                    and subscription.notice_timing == "cron"
                    and (due_notice is None or now >= due_notice)
                ):
                    await self._push_subscription(
                        subscription,
                        album,
                        subscription.pending_pids,
                        config,
                    )
                    subscription.pending_pids = []
                    subscription.next_notice_at = next_run(
                        "cron", subscription.notice_cron, now
                    ).isoformat(timespec="seconds")
                changed = True
            except JmDownloaderError as exc:
                self.logger.warning(
                    "订阅 %s 检查失败：%s", subscription.album_id, exc
                )
                subscription.next_run_at = next_run(
                    subscription.schedule_kind,
                    subscription.schedule_value,
                    now,
                ).isoformat(timespec="seconds")
                changed = True
            except Exception:  # noqa: BLE001 - 单个订阅失败不影响其它
                self.logger.exception(
                    "订阅 %s 检查异常", subscription.album_id
                )
                changed = True
        if changed:
            store.save(subscriptions)

    async def _push_subscription(
        self,
        subscription: Subscription,
        album: AlbumInfo,
        pids: list[str],
        config: PluginConfig,
    ) -> None:
        """向订阅目标推送更新文件或消息通知。"""
        by_pid = {chapter.pid: chapter for chapter in album.chapters}
        refs = tuple(by_pid[pid] for pid in pids if pid in by_pid)
        if not refs:
            return
        title = subscription.album_title or album.title
        summary = (
            f"JM{album.album_id}「{title}」有 {len(refs)} 章更新："
            + "、".join(pid for pid in pids[:5])
            + (" 等" if len(refs) > 5 else "")
        )
        if subscription.push_mode == "notice":
            for target in subscription.targets:
                await self._push_text(target, summary)
            return
        path: Path | None = None
        try:
            path, file_name, password = await self._build_subscription_file(
                subscription, album, refs, config
            )
            for target in subscription.targets:
                await self._push_file(target, path, file_name, config)
                text = summary + "，已推送文件"
                if password:
                    text += f"，打开密码：{password}"
                await self._push_text(target, text)
        except Exception as exc:  # noqa: BLE001 - 失败时退化为通知
            self.logger.warning(
                "订阅 %s 文件推送失败", subscription.album_id, exc_info=True
            )
            for target in subscription.targets:
                await self._push_text(
                    target, summary + f"（文件推送失败：{self._short_error(exc)}）"
                )
        finally:
            if path is not None:
                rmtree(path.parent, ignore_errors=True)

    async def _build_subscription_file(
        self,
        subscription: Subscription,
        album: AlbumInfo,
        refs: tuple[ChapterRef, ...],
        config: PluginConfig,
    ) -> tuple[Path, str, str]:
        """下载更新章节并合成订阅推送文件，返回 (路径, 文件名, 密码)。"""
        source = JmSource(config)
        cache = self._open_cache(config)
        job_root = Path(
            mkdtemp(prefix=f"jm{album.album_id}-subscription-", dir=str(JOB_TEMP_ROOT))
        )
        try:
            chapters, _stats = await self._collect_chapters(
                source,
                album,
                config,
                job_root,
                cache,
                refs,
                ignore_cache=False,
            )
            if not chapters:
                raise JmDownloaderError("订阅更新章节下载失败")
            password = resolve_pdf_password(config)
            if subscription.file_type == "pdf":
                path = await self._build_pdf(
                    chapters, job_root / "subscription.pdf", password
                )
                name = f"JM{album.album_id}-更新-{len(chapters)}章.pdf"
            else:
                fmt = subscription.file_type
                archive_password = password if fmt in {"zip", "7z"} else ""
                if password and not archive_password:
                    raise JmDownloaderError(
                        "tar 系列压缩包不支持密码，请改用 zip/7z 或关闭加密"
                    )
                members = [
                    (
                        chapter.path,
                        f"{order:03d}-{_filename_fragment(chapter.title, 40) or 'chapter'}.pdf",
                    )
                    for order, chapter in enumerate(chapters, start=1)
                ]
                path = job_root / f"subscription{archive_suffix(fmt)}"
                await to_thread(
                    build_archive, members, path, fmt, archive_password
                )
                name = (
                    f"JM{album.album_id}-更新-{len(chapters)}章"
                    f"{archive_suffix(fmt)}"
                )
            # 把最终文件移到 job_root 外一层，避免调用方清理时丢失
            final = Path(
                mkdtemp(prefix="jm-sub-push-", dir=str(JOB_TEMP_ROOT))
            ) / name
            path.replace(final)
            return final, name, password
        finally:
            rmtree(job_root, ignore_errors=True)

    async def _push_text(self, target: str, text: str) -> None:
        """向指定会话主动发送文字消息。"""
        try:
            await self.context.send_message(target, MessageChain().message(text))
        except Exception:  # noqa: BLE001 - 推送失败只记日志
            self.logger.warning("订阅消息推送失败：%s", target, exc_info=True)

    async def _push_file(
        self, target: str, path: Path, file_name: str, config: PluginConfig
    ) -> None:
        """通过目标平台适配器主动上传文件（当前支持 aiocqhttp）。"""
        parts = str(target).split(":", 2)
        if len(parts) != 3:
            raise RuntimeError(f"无法解析通知目标：{target}")
        platform_name, message_type, session_id = parts
        platform = None
        for candidate in self.context.platform_manager.platform_insts:
            if candidate.meta().id == platform_name:
                platform = candidate
                break
        if platform is None:
            raise RuntimeError(f"找不到平台适配器：{platform_name}")
        bot = getattr(platform, "bot", None)
        if bot is None:
            raise RuntimeError("当前平台不支持主动文件推送")
        event = _PushUploadEvent(
            target,
            message_type == "GroupMessage",
            session_id,
            bot,
        )
        await upload_pdf(event, path, file_name, config.callback_api_base)

    # ---- Web API（订阅管理页） -----------------------------------------

    async def web_subscriptions(self):
        from astrbot.api.web import json_response

        config = PluginConfig.from_mapping(self.raw_config)
        store = self._subscription_store()
        return json_response(
            {
                "subscriptions": [
                    item.to_dict() for item in (store.load() if store else [])
                ],
                "defaults": self._subscription_defaults(config),
                "config": {
                    "enabled": config.enabled,
                    "allow_group": config.allow_group,
                    "default_file_type": config.default_file_type,
                    "preview_pages": config.preview_pages,
                    "encrypt_pdf": config.encrypt_pdf,
                    "cache_albums": config.cache_albums,
                    "split_auto": config.split_auto,
                },
            }
        )

    async def web_subscription_save(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        album_id = normalize_album_id(str(payload.get("album_id", "")))
        if not album_id:
            return error_response("album_id 不合法")
        store = self._subscription_store()
        if store is None:
            return error_response("插件数据目录不可用")
        config = PluginConfig.from_mapping(self.raw_config)
        defaults = self._subscription_defaults(config)
        kind = payload.get("schedule_kind") or defaults["schedule_kind"]
        value = payload.get("schedule_value") or defaults["schedule_value"]
        try:
            if kind == "loop":
                value = f"{parse_loop_interval(value)}s"
            elif kind == "cron":
                value = validate_cron(value)
            else:
                return error_response("schedule_kind 只能是 loop 或 cron")
            notice_cron = validate_cron(
                payload.get("notice_cron") or defaults["notice_cron"]
            )
        except SubscriptionError as exc:
            return error_response(str(exc))
        targets = [
            str(item).strip()
            for item in payload.get("targets") or []
            if str(item).strip()
        ]
        if not targets:
            return error_response("targets 不能为空")
        file_type = parse_default_file_type(
            payload.get("file_type") or defaults["file_type"]
        )
        push_mode = str(
            payload.get("push_mode") or defaults["push_mode"]
        ).lower()
        if push_mode not in {"file", "notice"}:
            return error_response("push_mode 只能是 file 或 notice")
        timing = str(
            payload.get("notice_timing") or defaults["notice_timing"]
        ).lower()
        if timing not in {"realtime", "cron"}:
            return error_response("notice_timing 只能是 realtime 或 cron")
        now = datetime.now()
        existing = store.get(album_id)
        subscription = Subscription(
            album_id=album_id,
            album_title=str(payload.get("album_title") or ""),
            schedule_kind=kind,
            schedule_value=value,
            file_type=file_type,
            push_mode=push_mode,
            notice_type=str(payload.get("notice_type") or "group").lower(),
            targets=targets,
            notice_timing=timing,
            notice_cron=notice_cron,
            seen_pids=existing.seen_pids if existing else [],
            pending_pids=existing.pending_pids if existing else [],
            last_check_at=existing.last_check_at if existing else "",
            next_run_at=next_run(kind, value, now).isoformat(timespec="seconds"),
            next_notice_at=(
                next_run("cron", notice_cron, now).isoformat(timespec="seconds")
                if timing == "cron"
                else ""
            ),
            created_at=existing.created_at if existing else now_text(),
        )
        store.upsert(subscription)
        return json_response({"saved": True, "subscription": subscription.to_dict()})

    async def web_subscription_delete(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        album_id = normalize_album_id(str(payload.get("album_id", "")))
        if not album_id:
            return error_response("album_id 不合法")
        store = self._subscription_store()
        if store is None:
            return error_response("插件数据目录不可用")
        return json_response({"deleted": store.delete(album_id)})

    async def web_settings_save(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        store = self._subscription_store()
        if store is None:
            return error_response("插件数据目录不可用")
        allowed = {
            "schedule_kind",
            "schedule_value",
            "file_type",
            "push_mode",
            "notice_type",
            "notice_sids",
            "notice_timing",
            "notice_cron",
        }
        defaults = {
            key: payload[key]
            for key in allowed
            if key in payload and str(payload[key]).strip()
        }
        store.save_defaults(defaults)
        return json_response({"saved": True, "defaults": defaults})

    # ---- 任务主流程 ---------------------------------------------------

    async def _run_job(
        self,
        event: AstrMessageEvent,
        album_id: str,
        config: PluginConfig,
        job_root: Path,
        chapter_no: int | None = None,
        no_cache: bool = False,
        selector: ChapterQuery | None = None,
        file_type: str = "pdf",
        preview: int | None = None,
    ) -> str:
        """执行一次完整任务：读相册 → 收集章节 → 分模式投递。"""
        try:
            source = JmSource(config)
            album = await to_thread(source.fetch_album, album_id)
            suffix = "（强制无缓存拉取）" if no_cache else ""
            scope = self._plan_scope(album, chapter_no, selector, suffix)
            await self._say(event, scope.intro)

            cache = self._open_cache(config)
            chapters, stats = await self._collect_chapters(
                source,
                album,
                config,
                job_root,
                cache,
                scope.chapters,
                ignore_cache=no_cache,
            )
            if len(chapters) == 0:
                empty = (
                    f"{scope.label}没能下载，稍后重试即可。"
                    if scope.chapters is not None
                    else "所有章节都没能下载成功，稍后再试一次。"
                )
                raise PartialChapterError(empty, list(stats.failed_pids))

            cache_note = self._refresh_cache(
                cache,
                album,
                chapters,
                config,
                stats,
                merge_existing=scope.chapters is not None,
                forced_refresh=no_cache,
            )
            if stats.failed_pids:
                raise PartialChapterError(
                    self._partial_message(stats, len(chapters), cache),
                    list(stats.failed_pids),
                )

            context = _JobContext(
                event=event,
                album=album,
                config=config,
                job_root=job_root,
                chapters=chapters,
                stats=stats,
                password=resolve_pdf_password(config),
                cache_note=cache_note,
                selector=selector,
                label=scope.label,
            )
            if preview is not None:
                return await self._deliver_preview(
                    event,
                    source,
                    album,
                    scope,
                    config,
                    preview,
                    file_type,
                    job_root,
                )
            if file_type != "pdf":
                return await self._deliver_archive(context, file_type)
            if scope.chapters is not None:
                return await self._run_selected_mode(context)
            return await self._deliver_whole(context)
        except JmDownloaderError as exc:
            self.logger.warning("JM%s 任务失败：%s", album_id, exc)
            return f"JM{album_id} {exc.user_message}"
        except Exception:  # noqa: BLE001 - 兜底，避免命令静默失败
            self.logger.exception("JM%s 任务异常", album_id)
            return f"JM{album_id} 处理时出错，具体原因见 AstrBot 日志。"

    def _plan_scope(
        self,
        album: AlbumInfo,
        chapter_no: int | None,
        selector: ChapterQuery | None,
        suffix: str,
    ) -> _Scope:
        """把命令模式换算成待处理章节、展示文案与开头进度文案。"""
        title = _filename_fragment(album.title, 30)
        if chapter_no is not None:
            chapter = select_chapter(album, chapter_no)
            position = chapter_position(album, chapter.pid)
            chapter_title = _filename_fragment(chapter.title, 24)
            intro = (
                f"开始处理 JM{album.album_id}「{title}」第 {position}/"
                f"{album.chapter_count} 章「{chapter_title}」，"
                f"只下载该章{suffix}…"
            )
            return _Scope((chapter,), f"第 {position} 章", intro)
        if selector is not None:
            picked = resolve_selected_chapters(album, selector)
            label = selection_label(selector, len(picked), album.chapter_count)
            intro = (
                f"开始处理 JM{album.album_id}「{title}」"
                f"{label}{suffix}…"
            )
            return _Scope(picked, label, intro)
        intro = (
            f"开始处理 JM{album.album_id}「{title}」："
            f"共 {album.chapter_count} 章{suffix}…"
        )
        return _Scope(None, "", intro)

    @staticmethod
    def _partial_message(
        stats: JobStats, chapter_count: int, cache: ChapterCache | None
    ) -> str:
        """把部分章节失败整理成一句可续传的提示。"""
        preview = "、".join(stats.failed_pids[:3])
        if cache is None:
            resume = f"已完成 {chapter_count} 章，但缓存已关闭，重试需重新下载。"
        else:
            resume = f"已缓存 {chapter_count} 章，重试会从断点续传。"
        return (
            f"有 {len(stats.failed_pids)} 章下载失败（{preview}）；{resume}"
        )

    # ---- 整本 / 筛选 / 单章投递 ---------------------------------------

    async def _deliver_whole(self, context: _JobContext) -> str:
        """整本模式：合并（按需加密）后尝试单文件上传，失败进入分卷回退。"""
        delivery = self._delivery_mode(context.config)
        upload_path = await self._build_pdf(
            context.chapters, context.job_root / "whole.pdf", context.password
        )
        file_name = album_pdf_name(
            context.album.album_id, context.album.title
        )
        failure: BaseException | None = None
        try:
            # 整本只尝试一次：失败多半是协议端大文件问题，直接回退更省时间。
            await self._upload_once(
                context.event, upload_path, file_name, context.config
            )
        except Exception as exc:  # noqa: BLE001 - 失败后走分卷回退
            failure = exc
            self.logger.warning(
                "整本发送失败，转为分卷重试：%s",
                context.album_ref,
                exc_info=True,
            )
        if failure is None:
            size = upload_path.stat().st_size
            headline = (
                f"{context.album_ref} PDF 已作为 QQ 文件上传"
                f"（{format_mib(size)}{context.encrypt_note}）。"
            )
            return context.report(headline)
        return await self._run_split_fallback(
            context,
            total_size=upload_path.stat().st_size,
            trigger=failure,
            delivery=delivery,
            scope="整本",
        )

    async def _run_selected_mode(self, context: _JobContext) -> str:
        """章节号或章节筛选模式：单章直接发，多章合并后发。"""
        if len(context.chapters) == 1:
            only = context.chapters[0]
            position = chapter_position(context.album, only.pid)
            return await self._run_chapter_mode(context, position, only)
        upload_path = await self._build_pdf(
            context.chapters,
            context.job_root / "selection.pdf",
            context.password,
        )
        if context.selector is not None:
            file_name = selection_pdf_name(
                context.album.album_id,
                context.album.title,
                context.selector,
                len(context.chapters),
                context.album.chapter_count,
            )
        else:
            file_name = album_pdf_name(
                context.album.album_id, context.album.title
            )
        size = upload_path.stat().st_size
        failure: BaseException | None = None
        try:
            await self._upload_once(
                context.event, upload_path, file_name, context.config
            )
        except Exception as exc:  # noqa: BLE001 - 失败后走分卷回退
            failure = exc
            self.logger.warning(
                "所选章节发送失败，转为分卷重试：%s",
                context.album_ref,
                exc_info=True,
            )
        if failure is None:
            headline = (
                f"{context.album_ref} {context.label} PDF 已作为 QQ 文件上传"
                f"（{format_mib(size)}{context.encrypt_note}）。"
            )
            return context.report(headline)
        if not context.config.split_enabled:
            return (
                f"{context.album_ref} {context.label} PDF 已生成"
                f"（{format_mib(size)}{context.encrypt_note}），"
                "但发送失败，且配置里关闭了分卷回退。\n"
                f"原因：{self._short_error(failure)}"
            )
        return await self._run_split_fallback(
            context,
            total_size=size,
            trigger=failure,
            delivery=self._delivery_mode(context.config),
            scope=f"所选 {len(context.chapters)} 章",
        )

    async def _run_chapter_mode(
        self,
        context: _JobContext,
        chapter_number: int,
        chapter: ChapterPdf,
    ) -> str:
        """只发送指定的一章，不经过整本与分卷流程。"""
        label = f"{context.album_ref} 第 {chapter_number} 章"
        upload_path = chapter.path
        if context.password:
            encrypted = context.job_root / "chapter-encrypted.pdf"
            try:
                upload_path = await to_thread(
                    encrypt_pdf, chapter.path, encrypted, context.password
                )
            except Exception as exc:  # noqa: BLE001 - 加密失败不发明文
                raise PdfBuildError(
                    "PDF 加密失败，已取消上传"
                    "（请确认已安装 pypdf[crypto]）。"
                ) from exc
        size = upload_path.stat().st_size
        file_name = chapter_pdf_name(
            context.album.album_id,
            context.album.title,
            chapter_number,
            context.album.chapter_count,
            chapter.title,
        )
        try:
            await self._upload_with_retries(
                context.event, upload_path, file_name, context.config
            )
        except UploadConfigError as exc:
            return (
                f"{label} PDF 已生成"
                f"（{format_mib(size)}{context.encrypt_note}），"
                f"但无法上传：{exc.user_message}"
            )
        except Exception as exc:  # noqa: BLE001 - 单章失败直接回显原因
            self.logger.warning("%s 上传失败", label, exc_info=True)
            return (
                f"{label} PDF 已生成"
                f"（{format_mib(size)}{context.encrypt_note}），"
                f"但上传失败；原因：{self._short_error(exc)}；"
                "详因见 AstrBot 日志。"
            )
        self.logger.info(
            "单章 PDF 上传成功：%s（%s）", file_name, format_mib(size)
        )
        caption = _filename_fragment(chapter.title, 24) or "无标题"
        headline = (
            f"{label}「{caption}」PDF 已作为 QQ 文件上传"
            f"（{format_mib(size)}{context.encrypt_note}）。"
        )
        return context.report(headline)

    # ---- 分卷回退 -----------------------------------------------------

    async def _run_split_fallback(
        self,
        context: _JobContext,
        *,
        total_size: int,
        trigger: BaseException,
        delivery: DeliveryMode,
        scope: str = "整本",
    ) -> str:
        """整本/筛选上传失败后，按规划器给出的章节边界生成并上传分卷。"""
        if not context.config.split_enabled:
            return (
                f"{context.album_ref} {scope} PDF 已生成"
                f"（{format_mib(total_size)}），但上传失败，"
                "且配置中已关闭分卷回退。\n"
                f"原因：{self._short_error(trigger)}"
            )
        reason = self._short_error(trigger)
        manual_target = (
            0 if context.config.split_auto else context.config.split_size_mb
        )
        manual_cap = (
            0 if context.config.split_auto else context.config.split_max_volumes
        )
        split_input = SplitInput(
            chapters=tuple(context.chapters),
            whole_bytes=total_size,
            downloaded_bytes=context.stats.downloaded_image_bytes,
            delivery=delivery,
            manual_target_mb=manual_target,
            manual_max_volumes=manual_cap,
        )
        try:
            plan = await to_thread(plan_split, split_input)
        except SplitNotPossibleError as exc:
            return (
                f"{context.album_ref} {scope} PDF 已生成"
                f"（{format_mib(total_size)}），但上传失败且无法按章分卷："
                f"{exc.user_message}\n"
                f"原因：{reason}"
            )
        self.logger.info("JM%s 分卷规划：%s", context.album.album_id, plan.basis)
        await self._say(
            context.event,
            f"{scope} PDF 上传失败（{reason}），"
            f"按规划分 {plan.volume_count} 卷回退发送"
            f"（预计单卷 ≤{plan.target_mib:.0f} MiB"
            f"{context.encrypt_note}）…",
        )
        try:
            built = await self._build_volume_files(
                plan, context.chapters, context.password, context.job_root
            )
            built, plan = await self._replan_if_oversized(
                built,
                plan,
                split_input,
                context.chapters,
                context.password,
                context.job_root,
            )
        except JmDownloaderError as exc:
            return f"{context.album_ref} 分卷 PDF 生成失败：{exc.user_message}"

        uploaded, failures = await self._upload_volume_files(
            built, plan, context
        )
        if not uploaded:
            summary = (
                f"{context.album_ref} {scope}与分卷上传均失败"
                f"（共 {plan.volume_count} 卷）。"
            )
        elif failures:
            summary = (
                f"{context.album_ref} {scope}上传失败，回退分卷：已上传 "
                f"{len(uploaded)}/{plan.volume_count} 卷。"
            )
        else:
            summary = (
                f"{context.album_ref} {scope}上传失败，已按规划分 "
                f"{plan.volume_count} 卷发送完成"
                f"（预计单卷 ≤{plan.target_mib:.0f} MiB"
                f"{context.encrypt_note}）。"
            )
        lines = [summary]
        if failures:
            labels = "、".join(f"卷{number}" for number, _ in failures)
            lines.append(f"上传失败：{labels}；详因见 AstrBot 日志。")
            lines.append(f"原因：{failures[0][1][:160]}")
        if context.cache_note:
            lines.append(f"（{context.cache_note}）")
        if context.password and uploaded:
            lines.append(f"打开密码：{context.password}")
        return "\n".join(lines)

    async def _build_volume_files(
        self,
        plan: SplitPlan,
        chapters: list[ChapterPdf],
        password: str,
        job_root: Path,
    ) -> list[tuple[VolumePlan, Path]]:
        """按规划逐卷合并（并按需加密）章节 PDF。"""
        volume_dir = job_root / "volumes"
        volume_dir.mkdir(parents=True, exist_ok=True)
        built: list[tuple[VolumePlan, Path]] = []
        for volume in plan.volumes:
            plain = volume_dir / f"volume-{volume.number}.pdf"
            final = await self._build_pdf(
                list(volume.chapters), plain, password
            )
            built.append((volume, final))
        return built

    async def _replan_if_oversized(
        self,
        built: list[tuple[VolumePlan, Path]],
        plan: SplitPlan,
        split_input: SplitInput,
        chapters: list[ChapterPdf],
        password: str,
        job_root: Path,
    ) -> tuple[list[tuple[VolumePlan, Path]], SplitPlan]:
        """实际加密后的单卷超过硬上限时增卷重排（最多两次）。"""
        for _ in range(2):
            oversized = [
                item
                for item in built
                if item[1].stat().st_size > plan.hard_limit_bytes
            ]
            if not oversized or plan.volume_count >= len(chapters):
                break
            next_input = replace(
                split_input,
                min_volume_count=min(plan.volume_count + 1, len(chapters)),
            )
            next_plan = await to_thread(plan_split, next_input)
            if next_plan.volume_count <= plan.volume_count:
                break
            self.logger.info(
                "实际分卷超过硬上限，增卷重排：%d → %d 卷",
                plan.volume_count,
                next_plan.volume_count,
            )
            plan = next_plan
            built = await self._build_volume_files(
                plan, chapters, password, job_root
            )
        return built, plan

    async def _upload_volume_files(
        self,
        built: list[tuple[VolumePlan, Path]],
        plan: SplitPlan,
        context: _JobContext,
    ) -> tuple[list[int], list[tuple[int, str]]]:
        """逐卷上传；单卷失败只记录，不拖垮其他卷。"""
        sent: list[int] = []
        problems: list[tuple[int, str]] = []
        for volume, pdf_path in built:
            dial_name = volume_pdf_name(
                context.album.album_id,
                context.album.title,
                volume.number,
                plan.volume_count,
            )
            try:
                await self._upload_with_retries(
                    context.event, pdf_path, dial_name, context.config
                )
            except UploadConfigError as exc:
                problems.append((volume.number, str(exc)))
                self.logger.warning("卷 %d 未上传：%s", volume.number, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - 其余卷继续上传
                problems.append((volume.number, type(exc).__name__))
                self.logger.exception("卷 %d 上传失败", volume.number)
                continue
            sent.append(volume.number)
            self.logger.info(
                "分卷第 %d/%d 卷上传成功：%s（%s）",
                volume.number,
                plan.volume_count,
                dial_name,
                format_mib(pdf_path.stat().st_size),
            )
        return sent, problems

    # ---- 章节收集 -----------------------------------------------------

    async def _collect_chapters(
        self,
        source: JmSource,
        album: AlbumInfo,
        config: PluginConfig,
        job_root: Path,
        cache: ChapterCache | None,
        selected: tuple[ChapterRef, ...] | None = None,
        *,
        ignore_cache: bool = False,
    ) -> tuple[list[ChapterPdf], JobStats]:
        """复用缓存并补下载缺失章节，返回按相册顺序排列的章节 PDF。"""
        stats = JobStats()
        targets = list(album.chapters if selected is None else selected)
        store_dir = job_root / "chapters"
        cached_meta: dict[str, CachedChapter] = {}
        if cache:
            async with self._cache_lock:
                if not ignore_cache:
                    cached_meta = cache.load_chapters(album.album_id)
                store_dir = cache.chapters_dir(album.album_id)
        else:
            store_dir.mkdir(parents=True, exist_ok=True)

        ready: dict[int, ChapterPdf] = {}
        pending: list[tuple[int, ChapterRef]] = []
        for order, chapter in enumerate(targets):
            reused = self._reuse_cached(cache, album, chapter, cached_meta)
            if reused is None:
                pending.append((order, chapter))
            else:
                ready[order] = reused
                stats.reused_chapters += 1

        if pending:
            await self._download_missing(
                source, config, store_dir, job_root, pending, ready, stats
            )
        return [ready[order] for order in sorted(ready)], stats

    @staticmethod
    def _reuse_cached(
        cache: ChapterCache | None,
        album: AlbumInfo,
        chapter: ChapterRef,
        cached_meta: dict[str, CachedChapter],
    ) -> ChapterPdf | None:
        """缓存里有完整章节 PDF 时直接复用，否则返回 None 等下载。"""
        if cache is None:
            return None
        record = cached_meta.get(chapter.pid)
        if record is None:
            return None
        cached_file = cache.chapter_pdf_path(album.album_id, chapter.pid)
        if not cached_file.is_file():
            return None
        return ChapterPdf(
            pid=chapter.pid,
            index=chapter.index or record.index,
            title=chapter.title or record.title,
            page_count=record.page_count,
            size_bytes=cached_file.stat().st_size,
            path=cached_file,
        )

    async def _download_missing(
        self,
        source: JmSource,
        config: PluginConfig,
        store_dir: Path,
        job_root: Path,
        pending: list[tuple[int, ChapterRef]],
        ready: dict[int, ChapterPdf],
        stats: JobStats,
    ) -> None:
        """按配置的并发数补下载缺失章节，失败只记录不中断其他章节。"""
        gate = Semaphore(max(1, config.photo_threads))
        images_root = job_root / "images"

        async def fetch(order: int, chapter: ChapterRef) -> None:
            async with gate:
                target_pdf = store_dir / f"{chapter.pid}.pdf"
                try:
                    artifact = await to_thread(
                        source.download_chapter,
                        chapter.pid,
                        images_root,
                        target_pdf,
                    )
                except JmDownloaderError as exc:
                    stats.failed_pids.append(chapter.pid)
                    self.logger.warning("章节 %s 下载失败：%s", chapter.pid, exc)
                    return
                except Exception:  # noqa: BLE001 - 单章失败不影响其他章节
                    stats.failed_pids.append(chapter.pid)
                    self.logger.exception("章节 %s 下载异常", chapter.pid)
                    return
                ready[order] = ChapterPdf(
                    pid=chapter.pid,
                    index=chapter.index,
                    title=chapter.title,
                    page_count=artifact.page_count,
                    size_bytes=artifact.size_bytes,
                    path=artifact.pdf_path,
                )
                stats.downloaded_chapters += 1
                stats.downloaded_image_bytes += artifact.image_bytes

        await gather(*(fetch(order, chapter) for order, chapter in pending))

    # ---- 缓存 ---------------------------------------------------------

    def _open_cache(self, config: PluginConfig) -> ChapterCache | None:
        """缓存开关打开且数据目录可用时返回缓存实例。"""
        if config.cache_albums <= 0:
            return None
        root = resolve_cache_root(DEFAULT_PLUGIN_NAME)
        if root is not None:
            return ChapterCache(root)
        return None

    def _refresh_cache(
        self,
        cache: ChapterCache | None,
        album: AlbumInfo,
        chapters: list[ChapterPdf],
        config: PluginConfig,
        stats: JobStats,
        *,
        merge_existing: bool,
        forced_refresh: bool,
    ) -> str:
        """写缓存并返回给用户看的缓存说明；强制刷新时用固定文案。"""
        note = self._save_cache(
            cache,
            album,
            chapters,
            config,
            stats,
            merge_existing=merge_existing,
        )
        if not forced_refresh:
            return note
        if cache is None:
            return "本次强制无缓存拉取（缓存已关闭，未写缓存）"
        return "本次强制无缓存拉取，已用新结果刷新缓存"

    def _save_cache(
        self,
        cache: ChapterCache | None,
        album: AlbumInfo,
        chapters: list[ChapterPdf],
        config: PluginConfig,
        stats: JobStats,
        *,
        merge_existing: bool = False,
    ) -> str:
        """把本次章节元数据写入缓存，返回缓存状态说明。"""
        if cache is None:
            return ""
        try:
            records = [
                CachedChapter(
                    item.pid,
                    item.index,
                    item.title,
                    item.page_count,
                    item.size_bytes,
                )
                for item in chapters
            ]
            if merge_existing:
                # 单章/筛选模式只更新选中的章节，不能覆盖同一本子的其他条目。
                existing = cache.load_chapters(album.album_id)
                existing.update({record.pid: record for record in records})
                records = list(existing.values())
            cache.save_album(album.album_id, album.title, records)
            cache.touch(album.album_id)
            removed = cache.evict(config.cache_albums)
        except OSError:
            self.logger.warning("写入章节缓存失败", exc_info=True)
            return ""
        if removed:
            self.logger.info("缓存淘汰本子：%s", ", ".join(removed))
        return self._cache_note(stats, len(chapters))

    @staticmethod
    def _cache_note(stats: JobStats, chapter_count: int) -> str:
        """描述本次章节来自缓存还是新下载。"""
        if stats.downloaded_chapters == 0:
            if chapter_count == 1:
                return "本章复用缓存"
            return f"全部 {chapter_count} 章复用缓存"
        image_size = f"图片 {stats.total_image_mib:.1f} MiB"
        if chapter_count == 1:
            return f"本章新下载（{image_size}）"
        return (
            f"缓存复用 {stats.reused_chapters} 章 / 新下载 "
            f"{stats.downloaded_chapters} 章（{image_size}）"
        )

    # ---- 上传 ---------------------------------------------------------

    async def _upload_once(
        self,
        msg_event: AstrMessageEvent,
        pdf: Path,
        display_name: str,
        config: PluginConfig,
    ) -> object:
        """把 PDF 注册成协议端可读的 URI 并调用 OneBot 上传。"""
        base = resolve_callback_base(config.callback_api_base)
        return await upload_pdf(msg_event, pdf, display_name, base)

    async def _upload_with_retries(
        self,
        msg_event: AstrMessageEvent,
        pdf: Path,
        display_name: str,
        config: PluginConfig,
    ) -> object:
        """按配置次数重试上传；上传前置条件错误不重试。"""
        attempts = config.upload_retries + 1
        cause: BaseException | None = None
        for number in range(1, attempts + 1):
            try:
                return await self._upload_once(
                    msg_event, pdf, display_name, config
                )
            except UploadConfigError:  # 配置不满足时重试也没有意义
                raise
            except Exception as exc:  # noqa: BLE001 - 退避后统一抛出
                cause = exc
                self.logger.warning(
                    "第 %d/%d 次上传未成功：%s",
                    number,
                    attempts,
                    display_name,
                    exc_info=number == attempts,
                )
                if number < attempts:
                    await sleep(3 + (number - 1) * 2)
        if cause is None:  # pragma: no cover - 循环至少执行一次
            raise RuntimeError("上传未执行")
        raise cause

    # ---- 预览 ---------------------------------------------------------

    async def _deliver_preview(
        self,
        event: AstrMessageEvent,
        source: JmSource,
        album: AlbumInfo,
        scope: "_Scope",
        config: PluginConfig,
        requested: int,
        file_type: str,
        job_root: Path,
    ) -> str:
        """取所选范围第一章的前 N 页，按指定/默认格式生成预览文件并上传。"""
        chapters = scope.chapters if scope.chapters is not None else album.chapters
        first = chapters[0]
        count = requested if requested not in (None, 0) else config.preview_pages
        count = max(1, min(50, int(count)))
        position = chapter_position(album, first.pid)
        title = _filename_fragment(first.title, 24) or "无标题"
        await self._say(
            event,
            f"JM{album.album_id} 第 {position} 章「{title}」预览："
            f"正在获取前 {count} 页…",
        )
        raw_pdf = job_root / "preview.pdf"
        page_count, _ = await to_thread(
            source.preview_pdf,
            first.pid,
            job_root / "preview-images" / first.pid,
            raw_pdf,
            count,
        )
        pdf_name = preview_pdf_name(
            album.album_id,
            album.title,
            position,
            album.chapter_count,
            first.title,
            page_count,
        )
        password = config.pdf_password if config.encrypt_pdf else ""
        if not config.encrypt_pdf:
            password = ""
        elif not password:
            password = generate_pdf_password()

        if file_type == "pdf":
            upload_path = raw_pdf
            if password:
                encrypted = job_root / "preview-encrypted.pdf"
                try:
                    upload_path = await to_thread(
                        encrypt_pdf, raw_pdf, encrypted, password
                    )
                except Exception as exc:  # noqa: BLE001 - 加密失败不发明文
                    raise PdfBuildError(
                        "PDF 加密失败，已取消上传"
                        "（请确认已安装 pypdf[crypto]）。"
                    ) from exc
            file_name = pdf_name
        else:
            fmt = file_type
            archive_password = password if fmt in {"zip", "7z"} else ""
            if password and not archive_password:
                return (
                    f"{album_ref(album)} 预览文件已生成（{page_count} 页），"
                    "但 tar 系列压缩包不支持设置密码；"
                    "请改用 --type zip/7z，或关闭 PDF 加密后再使用 tar 系列。"
                )
            archive_path = job_root / f"preview{archive_suffix(fmt)}"
            await to_thread(
                build_archive,
                [(raw_pdf, pdf_name)],
                archive_path,
                fmt,
                archive_password,
            )
            upload_path = archive_path
            file_name = pdf_name[: -len(".pdf")] + archive_suffix(fmt)
        size = upload_path.stat().st_size
        try:
            await self._upload_with_retries(
                event, upload_path, file_name, config
            )
        except UploadConfigError as exc:
            return (
                f"{album_ref(album)} 第 {position} 章预览（{page_count} 页，"
                f"{file_type}）已生成，但无法上传：{exc.user_message}"
            )
        except Exception as exc:  # noqa: BLE001 - 上传失败回显原因
            self.logger.warning(
                "%s 预览上传失败", album_ref(album), exc_info=True
            )
            return (
                f"{album_ref(album)} 第 {position} 章预览（{page_count} 页，"
                f"{file_type}）已生成，但上传失败；原因："
                f"{self._short_error(exc)}；详因见 AstrBot 日志。"
            )
        encrypt_note = "，已加密" if password else ""
        return (
            f"{album_ref(album)} 第 {position} 章「{title}」预览"
            f"（前 {page_count} 页，{file_type}）已作为 QQ 文件上传"
            f"（{format_mib(size)}{encrypt_note}）。"
        )

    # ---- 压缩包投递 ---------------------------------------------------
    # ---- 压缩包投递 ---------------------------------------------------

    @staticmethod
    def _archive_file_name(context: "_JobContext", fmt: str) -> str:
        """压缩包上传文件名：沿用 PDF 基名，替换为压缩后缀。"""
        if len(context.chapters) == 1:
            chapter = context.chapters[0]
            position = chapter_position(context.album, chapter.pid)
            pdf_name = chapter_pdf_name(
                context.album.album_id,
                context.album.title,
                position,
                context.album.chapter_count,
                chapter.title,
            )
        elif context.selector is not None:
            pdf_name = selection_pdf_name(
                context.album.album_id,
                context.album.title,
                context.selector,
                len(context.chapters),
                context.album.chapter_count,
            )
        else:
            pdf_name = album_pdf_name(
                context.album.album_id, context.album.title
            )
        return pdf_name[: -len(".pdf")] + archive_suffix(fmt)

    async def _build_archive(
        self, context: "_JobContext", fmt: str
    ) -> Path:
        """把章节 PDF 打包为压缩包；zip/7z 在包级别加密。"""
        archive_password = context.password if fmt in {"zip", "7z"} else ""
        if context.password and not archive_password:
            raise ArchiveBuildError(
                "tar 系列压缩包不支持设置密码；请改用 --type zip/7z，"
                "或关闭 PDF 加密后再使用 tar 系列。"
            )
        members: list[tuple[Path, str]] = []
        for order, chapter in enumerate(context.chapters, start=1):
            title = _filename_fragment(chapter.title, 40)
            title = title.replace("/", "_").replace("\\", "_").strip() or "chapter"
            members.append((chapter.path, f"{order:03d}-{title}.pdf"))
        output = context.job_root / f"bundle{archive_suffix(fmt)}"
        return await to_thread(
            build_archive, members, output, fmt, archive_password
        )

    async def _deliver_archive(
        self, context: "_JobContext", fmt: str
    ) -> str:
        """生成压缩包并上传；压缩包模式不进入 PDF 分卷回退。"""
        label = context.label or "整本"
        try:
            archive_path = await self._build_archive(context, fmt)
        except JmDownloaderError as exc:
            return f"{context.album_ref} 压缩包生成失败：{exc.user_message}"
        size = archive_path.stat().st_size
        file_name = self._archive_file_name(context, fmt)
        try:
            await self._upload_with_retries(
                context.event, archive_path, file_name, context.config
            )
        except UploadConfigError as exc:
            return (
                f"{context.album_ref} {label} {fmt} 压缩包已生成"
                f"（{format_mib(size)}{context.encrypt_note}），"
                f"但无法上传：{exc.user_message}"
            )
        except Exception as exc:  # noqa: BLE001 - 压缩包上传失败直接回显原因
            self.logger.warning(
                "%s 压缩包上传失败", context.album_ref, exc_info=True
            )
            return (
                f"{context.album_ref} {label} {fmt} 压缩包已生成"
                f"（{format_mib(size)}{context.encrypt_note}），"
                f"但上传失败；原因：{self._short_error(exc)}；"
                "详因见 AstrBot 日志。"
            )
        self.logger.info(
            "压缩包上传成功：%s（%s）", file_name, format_mib(size)
        )
        return context.report(
            f"{context.album_ref} {label} {fmt} 压缩包已作为 QQ 文件上传"
            f"（{format_mib(size)}{context.encrypt_note}）。"
        )

    # ---- 工具 ---------------------------------------------------------

    async def _build_pdf(
        self, chapters: list[ChapterPdf], output: Path, password: str
    ) -> Path:
        """合并章节 PDF 并按需加密，返回最终待上传文件。"""
        await to_thread(
            merge_pdfs, [chapter.path for chapter in chapters], output
        )
        if password:
            encrypted = output.with_name(f"{output.stem}-encrypted.pdf")
            try:
                return await to_thread(encrypt_pdf, output, encrypted, password)
            except Exception as exc:  # noqa: BLE001 - 加密失败不发明文
                raise PdfBuildError(
                    "PDF 加密失败，已取消上传"
                    "（请确认已安装 pypdf[crypto]）。"
                ) from exc
        return output

    @staticmethod
    def _delivery_mode(config: PluginConfig) -> DeliveryMode:
        """协议端可达时用文件服务 URL，否则回退 base64。"""
        if resolve_callback_base(config.callback_api_base):
            return DeliveryMode.FILE_URL
        return DeliveryMode.BASE64

    @staticmethod
    def _short_error(exc: BaseException) -> str:
        """提取适合放进单行消息的简短错误原因。"""
        message = str(exc).strip() or type(exc).__name__
        return " ".join(message.split())[:140]

    @staticmethod
    def _cleanup_stale_jobs() -> None:
        """清理上次进程遗留的临时任务目录。"""
        deadline = time() - STALE_JOB_SECONDS
        try:
            entries = [
                entry for entry in JOB_TEMP_ROOT.iterdir() if entry.is_dir()
            ]
        except OSError:  # 临时目录不可用时直接放弃本次清理
            return
        for entry in entries:
            try:
                expired = entry.stat().st_mtime < deadline
            except OSError:  # 目录可能在扫描与 stat 之间被删除
                continue
            if expired:
                rmtree(entry, ignore_errors=True)
