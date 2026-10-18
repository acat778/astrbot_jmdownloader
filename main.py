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

import re
from asyncio import Lock, Semaphore, gather, sleep, to_thread
from dataclasses import dataclass, replace
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
from .core.config import DEFAULT_PLUGIN_NAME, PluginConfig
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
    JobStats,
    MIB,
    SplitPlan,
    VolumePlan,
)
from .core.pdf import encrypt_pdf, merge_pdfs
from .core.planner import SplitInput, plan_split

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
    预览模式：取所选范围（整本/单章/一条范围）第一章的前 N 张图片直接发送；
    N 省略或写 0 时使用配置 preview_images（默认 10）。可与 --type 和章节筛选混用。

规则：
1. -- 开头的开关可以放在 /jm 后的任意位置，例如 /jm --no-cache 123456。
2. 章节号与 --head/--tail/--latest/--range 不能同时使用，筛选开关之间也互斥。
3. --no-cache 可以和任意章节筛选组合，例如 /jm 123456 --head 3 --no-cache。
4. --type 可以放在任意位置，例如 /jm 123456 --head 3 --type zip。
5. --pre 可与 --type、--head/--tail/--latest/--range 组合，顺序不限；预览模式只发图片，不发 PDF/压缩包。
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
    "--type <pdf|zip|7z|tar|tar.gz|tar.bz2|tar.xz> | --pre [N]]；"
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
    cursor = 0
    while cursor < len(words):
        word = words[cursor]
        cursor += 1
        if not word.startswith("--"):
            positionals.append(word)
            continue
        flag = word.lower()
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
    return CommandRequest(
        positionals[0],
        chapter_no,
        selector,
        no_cache,
        file_type,
        preview,
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
        JOB_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_jobs()

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

    async def _render_help_image(
        self, event: AstrMessageEvent
    ) -> str | None:
        """调用 AstrBot 的文本转图服务，失败返回 None。"""
        try:
            umo = getattr(event, "unified_msg_origin", None)
            rendered = await self.text_to_image(
                HELP_TEXT, return_url=True, umo=umo
            )
        except Exception:  # noqa: BLE001 - t2i 不可用/未配置
            self.logger.warning("帮助内容转图失败，改用文字版", exc_info=True)
            return None
        return rendered or None

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
        job_root: Path,
    ) -> str:
        """取所选范围第一章的前 N 张图片直接发送，不生成 PDF/压缩包。"""
        chapters = scope.chapters if scope.chapters is not None else album.chapters
        first = chapters[0]
        count = requested if requested not in (None, 0) else config.preview_images
        count = max(1, min(50, int(count)))
        position = chapter_position(album, first.pid)
        title = _filename_fragment(first.title, 24) or "无标题"
        await self._say(
            event,
            f"JM{album.album_id} 第 {position} 章「{title}」预览："
            f"正在获取前 {count} 张图片…",
        )
        images = await to_thread(
            source.preview_images,
            first.pid,
            job_root / "preview" / first.pid,
            count,
        )
        sent = 0
        for image in images:
            try:
                await event.send(MessageChain().file_image(str(image)))
            except Exception:  # noqa: BLE001 - 单张图片发送失败就停止
                self.logger.warning("预览图片发送失败", exc_info=True)
                break
            sent += 1
        return (
            f"JM{album.album_id} 第 {position} 章「{title}」预览完成："
            f"已发送 {sent} 张（请求前 {count} 张）。"
        )

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
