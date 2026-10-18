"""插件入口辅助函数与配置快照测试。"""

from __future__ import annotations

import asyncio
import tarfile
import inspect
from pathlib import Path

import pytest

from astrbot_jmdownloader.core.cache import ChapterCache
from astrbot_jmdownloader.core.config import PluginConfig
from astrbot_jmdownloader.core.errors import ChapterOutOfRangeError
from astrbot_jmdownloader.core.models import (
    AlbumInfo,
    ChapterArtifact,
    ChapterPdf,
    ChapterQuery,
    ChapterRef,
)


def test_normalize_album_id(plugin_main):
    assert plugin_main.normalize_album_id("123456") == "123456"
    assert plugin_main.normalize_album_id("JM123456") == "123456"
    assert plugin_main.normalize_album_id("jm 123456") == "123456"
    assert plugin_main.normalize_album_id("abc") is None
    assert plugin_main.normalize_album_id("") is None
    assert plugin_main.normalize_album_id("12") is None


def test_generate_pdf_password(plugin_main):
    first = plugin_main.generate_pdf_password()
    second = plugin_main.generate_pdf_password()
    assert len(first) == plugin_main.PDF_PASSWORD_LENGTH
    assert set(first) <= set(plugin_main.PASSWORD_ALPHABET)
    assert first != second


def test_pdf_names(plugin_main):
    assert plugin_main.album_pdf_name("123456", "") == "JM123456.pdf"
    assert plugin_main.album_pdf_name("123456", "标题") == "JM123456-标题.pdf"
    assert (
        plugin_main.volume_pdf_name("123456", "", 1, 1) == "JM123456.pdf"
    )
    assert (
        plugin_main.volume_pdf_name("123456", "标题/非法:字符", 2, 3)
        == "JM123456-卷2of3-标题_非法_字符.pdf"
    )


def test_short_error(plugin_main):
    short_error = plugin_main.JmDownloaderPlugin._short_error
    assert short_error(RuntimeError("  第一行\n第二行  ")) == "第一行 第二行"
    assert short_error(ValueError("")) == "ValueError"


def test_format_mib(plugin_main):
    assert plugin_main.format_mib(0) == "0.00 MiB"
    assert plugin_main.format_mib(2 * 1024 * 1024) == "2.00 MiB"


def test_plugin_instantiation_reads_config(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"max_concurrent": 2, "encrypt_pdf": False}
    )
    config = PluginConfig.from_mapping(plugin.raw_config)
    assert config.max_concurrent == 2
    assert config.encrypt_pdf is False
    assert plugin._semaphore._value == 2  # noqa: SLF001 - 测试内部状态


def test_save_cache_none_returns_empty(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    assert plugin._save_cache(None, None, [], PluginConfig(), None) == ""


def test_command_handler_is_coroutine(plugin_main):
    # 必须是协程 handler：异步生成器 + stop_event 会让管道在首个 yield 后
    # break 且不 aclose，清理与信号量释放被推迟到 GC。
    handler = plugin_main.JmDownloaderPlugin.download_command
    assert inspect.iscoroutinefunction(handler)
    assert not inspect.isasyncgenfunction(handler)


class FakeEvent:
    def __init__(self) -> None:
        self.group_id = ""
        self.unified_msg_origin = "aiocqhttp:GroupMessage:999"
        self.stopped = False
        self.messages: list[str] = []
        self.chains: list = []

    def stop_event(self) -> None:
        self.stopped = True

    def get_group_id(self) -> str:
        return self.group_id

    async def send(self, chain) -> None:
        self.chains.append(chain)
        self.messages.append(chain.text)


def test_invalid_usage_sends_message_and_stops(plugin_main):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()
    asyncio.run(plugin.download_command(event, "abc"))
    assert event.stopped is True
    assert len(event.messages) == 1
    assert "用法" in event.messages[0]


def test_command_releases_semaphore_after_job(plugin_main, monkeypatch):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(None, {"max_concurrent": 1})

    async def fake_run_job(
        event,
        album_id,
        config,
        job_root,
        chapter_no=None,
        no_cache=False,
        selector=None,
        file_type="pdf",
        preview=None,
    ) -> str:
        assert plugin._semaphore.locked() is True  # noqa: SLF001
        return "完成"

    monkeypatch.setattr(plugin, "_run_job", fake_run_job)
    event = FakeEvent()
    asyncio.run(plugin.download_command(event, "123456"))
    # 协程 handler 完整执行到返回，信号量必须在返回前释放
    assert plugin._semaphore.locked() is False  # noqa: SLF001
    assert event.messages == ["完成"]


def test_group_whitelist_blocks_unlisted_group(plugin_main):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(
        None, {"groups": {"group_whitelist": "111"}}
    )
    event = FakeEvent()
    event.group_id = "222"
    asyncio.run(plugin.download_command(event, "123456"))
    assert len(event.messages) == 1
    assert "白名单" in event.messages[0]


def test_group_blacklist_blocks_group(plugin_main):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(
        None, {"groups": {"group_blacklist": "222"}}
    )
    event = FakeEvent()
    event.group_id = "222"
    asyncio.run(plugin.download_command(event, "help"))
    assert len(event.messages) == 1
    assert "黑名单" in event.messages[0]


def test_group_lists_empty_allow_group(plugin_main):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()
    event.group_id = "333"
    asyncio.run(plugin.download_command(event, "abc"))
    assert len(event.messages) == 1
    assert "用法" in event.messages[0]


def test_group_lists_do_not_apply_to_private_chat(plugin_main):
    import asyncio

    plugin = plugin_main.JmDownloaderPlugin(
        None, {"groups": {"group_blacklist": "333"}}
    )
    event = FakeEvent()
    asyncio.run(plugin.download_command(event, "abc"))
    assert len(event.messages) == 1
    assert "用法" in event.messages[0]


def test_subscription_command_saves_subscription(
    plugin_main, monkeypatch, tmp_path
):
    from astrbot_jmdownloader.core.subscriptions import SubscriptionStore

    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    plugin._sub_store_cache = SubscriptionStore(tmp_path)  # noqa: SLF001
    event = FakeEvent()
    text = asyncio.run(
        plugin._run_command(
            event,
            PluginConfig(),
            "123456 --sub loop 6h --type zip --push notice "
            "--notice group realtime",
        )
    )
    assert "已订阅" in text
    saved = plugin._sub_store_cache.load()  # noqa: SLF001
    assert len(saved) == 1
    assert saved[0].album_id == "999"
    assert saved[0].schedule_kind == "loop"
    assert saved[0].schedule_value == "21600s"
    assert saved[0].file_type == "zip"
    assert saved[0].push_mode == "notice"
    assert saved[0].targets == ["aiocqhttp:GroupMessage:999"]
    # 取消订阅
    text = asyncio.run(plugin._run_command(event, PluginConfig(), "999 --sub off"))
    assert "已取消" in text
    assert plugin._sub_store_cache.load() == []  # noqa: SLF001


def test_info_command_returns_detail_text_fallback(
    plugin_main, monkeypatch
):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    text = asyncio.run(plugin._run_command(FakeEvent(), PluginConfig(), "999 --info"))
    assert "JM999" in text and "测试本" in text
    assert "章节 3" in text
    assert "详情页" in text


def test_info_command_sends_cover_image(plugin_main, monkeypatch, tmp_path):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {})

    async def fake_render(event, text):
        return None

    monkeypatch.setattr(plugin, "_render_text_image", fake_render)
    event = FakeEvent()
    text = asyncio.run(plugin._run_command(event, PluginConfig(), "999 --info"))
    cover_chains = [chain for chain in event.chains if chain.images]
    assert len(cover_chains) == 1
    assert cover_chains[0].images[0][0] == "file"
    assert "测试本" in text


def test_info_command_renders_cover_and_detail_as_one_image(
    plugin_main, monkeypatch
):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()
    rendered_calls: list[str] = []

    async def fake_html_render(tmpl, data, **kwargs):
        rendered_calls.append(data["title"])
        assert data["cover"].startswith("data:image/jpeg;base64,")
        return "https://example.invalid/info-card.jpg"

    monkeypatch.setattr(plugin, "html_render", fake_html_render, raising=False)
    reply = asyncio.run(plugin._run_command(event, PluginConfig(), "999 --info"))
    assert reply == ""
    assert rendered_calls == ["测试本"]
    assert len(event.chains) == 1  # 封面+详情合成为一张图
    assert event.chains[0].images == [
        ("url", "https://example.invalid/info-card.jpg")
    ]


def test_run_job_and_fallback_are_coroutines(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    assert inspect.iscoroutinefunction(plugin._run_job)  # noqa: SLF001
    assert inspect.iscoroutinefunction(plugin._run_split_fallback)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 章节号 / --no-cache / 单章模式
# ---------------------------------------------------------------------------


def _album(album_id: str = "999", count: int = 3) -> AlbumInfo:
    return AlbumInfo(
        album_id=album_id,
        title="测试本",
        chapters=tuple(
            ChapterRef(
                pid=f"p{index}",
                index=str(index),
                title=f"chapter-{index}",
            )
            for index in range(1, count + 1)
        ),
    )


def test_normalize_chapter_no(plugin_main):
    normalize = plugin_main.normalize_chapter_no
    assert normalize("") is None
    for raw in ("3", "第3章", "ch3", "chapter 3", " 3 "):
        assert normalize(raw) == 3
    for raw in ("0", "abc", "3.5", "--no-cache"):
        with pytest.raises(ValueError):
            normalize(raw)


def test_run_command_uses_configured_default_file_type(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    calls: list[tuple] = []

    async def fake_run_job(
        event,
        album_id,
        config,
        job_root,
        chapter_no=None,
        no_cache=False,
        selector=None,
        file_type="pdf",
        preview=None,
    ) -> str:
        calls.append((album_id, file_type, preview))
        return "ok"

    plugin._run_job = fake_run_job  # type: ignore[assignment]
    event = FakeEvent()
    configured = PluginConfig.from_mapping(
        {"basic": {"default_file_type": "zip"}}
    )
    assert asyncio.run(plugin._run_command(event, configured, "123456")) == "ok"
    assert calls[-1] == ("123456", "zip", None)
    # 显式 --type 覆盖配置默认值
    assert (
        asyncio.run(plugin._run_command(event, configured, "123456 --type 7z"))
        == "ok"
    )
    assert calls[-1] == ("123456", "7z", None)
    # 未知默认格式回退 pdf
    fallback = PluginConfig.from_mapping(
        {"basic": {"default_file_type": "rar"}}
    )
    assert asyncio.run(plugin._run_command(event, fallback, "123456")) == "ok"
    assert calls[-1] == ("123456", "pdf", None)


def test_parse_command_arguments(plugin_main):
    from astrbot_jmdownloader.core.models import ChapterQuery, CommandRequest

    parse = plugin_main.parse_command_arguments
    assert parse("123456") == CommandRequest("123456")
    assert parse("123456 3") == CommandRequest("123456", chapter_no=3)
    assert parse("123456 第3章") == CommandRequest("123456", chapter_no=3)
    assert parse("123456 --no-cache") == CommandRequest(
        "123456", no_cache=True
    )
    assert parse("--no-cache 123456") == CommandRequest(
        "123456", no_cache=True
    )
    assert parse("123456 3 --no-cache") == CommandRequest(
        "123456", chapter_no=3, no_cache=True
    )
    assert parse("123456 --no-cache 3") == CommandRequest(
        "123456", chapter_no=3, no_cache=True
    )
    assert parse("123456 --head") == CommandRequest(
        "123456", selector=ChapterQuery("head", 1)
    )
    assert parse("123456 --head 3") == CommandRequest(
        "123456", selector=ChapterQuery("head", 3)
    )
    assert parse("--tail 2 123456") == CommandRequest(
        "123456", selector=ChapterQuery("tail", 2)
    )
    assert parse("123456 --latest 5 --no-cache") == CommandRequest(
        "123456",
        selector=ChapterQuery("latest", 5),
        no_cache=True,
    )
    assert parse("123456 --range 5") == CommandRequest(
        "123456", selector=ChapterQuery("range", 5, 1)
    )
    assert parse("123456 --range 5 3") == CommandRequest(
        "123456", selector=ChapterQuery("range", 5, 3)
    )
    assert parse("123456 --range 5 -1") == CommandRequest(
        "123456", selector=ChapterQuery("range", 5, -1)
    )
    assert parse("123456 --type zip") == CommandRequest(
        "123456", file_type="zip"
    )
    assert parse("123456 --type=tar.gz --head 2") == CommandRequest(
        "123456", selector=ChapterQuery("head", 2), file_type="tar.gz"
    )
    assert parse("--type 7z 123456 3") == CommandRequest(
        "123456", chapter_no=3, file_type="7z"
    )
    assert parse("123456 --type pdf") == CommandRequest(
        "123456", file_type="pdf"
    )
    assert parse("123456 --info").info is True
    assert parse("--info 123456").info is True
    assert parse("123456 --info --type zip").info is True
    with pytest.raises(ValueError):
        parse("123456 --type rar")
    with pytest.raises(ValueError):
        parse("123456 --type")
    assert parse("123456 --pre") == CommandRequest(
        "123456", preview=0
    )
    assert parse("123456 --pre 5") == CommandRequest(
        "123456", preview=5
    )
    assert parse("123456 --pre=5 --head 2") == CommandRequest(
        "123456", selector=ChapterQuery("head", 2), preview=5
    )
    assert parse("--pre 5 123456") == CommandRequest("123456", preview=5)
    assert parse("123456 --pre 0") == CommandRequest("123456", preview=0)
    with pytest.raises(ValueError):
        parse("123456 --pre 99")
    with pytest.raises(ValueError):
        parse("123456 --pre=abc")
    sub = parse(
        "123456 --sub cron 0 9 * * * --type zip --push file "
        "--notice group 111,222 realtime"
    )
    assert sub.subscription is not None
    assert sub.subscription.schedule_kind == "cron"
    assert sub.subscription.schedule_value == "0 9 * * *"
    assert sub.subscription.file_type == "zip"
    assert sub.subscription.push_mode == "file"
    assert sub.subscription.notice_type == "group"
    assert sub.subscription.notice_sids == "111,222"
    assert sub.subscription.notice_timing == "realtime"
    assert sub.file_type is None

    loop_sub = parse("123456 --sub loop 30m")
    assert loop_sub.subscription is not None
    assert loop_sub.subscription.schedule_kind == "loop"
    assert loop_sub.subscription.schedule_value == "30m"

    notice_cron = parse(
        "123456 --sub loop 1h --notice user 333 cron 0 9 * * *"
    )
    assert notice_cron.subscription is not None
    assert notice_cron.subscription.notice_timing == "cron"
    assert notice_cron.subscription.notice_cron == "0 9 * * *"

    assert parse("123456 --sub off").subscription is not None



    with pytest.raises(ValueError):
        parse("")
    with pytest.raises(ValueError):
        parse("123456 --unknown")
    with pytest.raises(ValueError):
        parse("123456 3 4")
    with pytest.raises(ValueError):
        parse("123456 abc")
    with pytest.raises(ValueError):
        parse("123456 --head --tail")
    with pytest.raises(ValueError):
        parse("123456 3 --head 2")
    with pytest.raises(ValueError):
        parse("123456 --range")  # 缺起始章
    with pytest.raises(ValueError):
        parse("123456 --range 5 0")
    with pytest.raises(ValueError):
        parse("123456 --head 0")


def test_select_chapter(plugin_main):
    album = _album()
    assert plugin_main.select_chapter(album, 2).pid == "p2"
    with pytest.raises(ChapterOutOfRangeError):
        plugin_main.select_chapter(album, 0)
    with pytest.raises(ChapterOutOfRangeError):
        plugin_main.select_chapter(album, 4)

    # 章节 index 与顺序不一致时优先按 index 匹配
    album = AlbumInfo(
        album_id="999",
        title="测试本",
        chapters=(
            ChapterRef("a", "5", "chapter-5"),
            ChapterRef("b", "6", "chapter-6"),
        ),
    )
    assert plugin_main.select_chapter(album, 6).pid == "b"


def test_no_cache_combines_with_all_selector_flags(plugin_main):
    from astrbot_jmdownloader.core.models import ChapterQuery, CommandRequest

    parse = plugin_main.parse_command_arguments
    assert parse("123456 --no-cache --head 2") == CommandRequest(
        "123456", selector=ChapterQuery("head", 2), no_cache=True
    )
    assert parse("123456 --head 2 --no-cache") == CommandRequest(
        "123456", selector=ChapterQuery("head", 2), no_cache=True
    )
    assert parse("123456 --no-cache --tail 2") == CommandRequest(
        "123456", selector=ChapterQuery("tail", 2), no_cache=True
    )
    assert parse("123456 --latest 2 --no-cache") == CommandRequest(
        "123456", selector=ChapterQuery("latest", 2), no_cache=True
    )
    assert parse("123456 --no-cache --range 3 2") == CommandRequest(
        "123456", selector=ChapterQuery("range", 3, 2), no_cache=True
    )
    assert parse("123456 --range 3 -1 --no-cache") == CommandRequest(
        "123456", selector=ChapterQuery("range", 3, -1), no_cache=True
    )
    assert parse("123456 3 --no-cache") == CommandRequest(
        "123456", chapter_no=3, no_cache=True
    )
    # 不同筛选参数之间仍互斥
    with pytest.raises(ValueError):
        parse("123456 --head 2 --tail 2")
    with pytest.raises(ValueError):
        parse("123456 3 --range 2 2")


def test_resolve_selected_chapters(plugin_main):
    album = _album(count=6)

    head = plugin_main.resolve_selected_chapters(album, ChapterQuery("head", 2))
    assert [chapter.pid for chapter in head] == ["p1", "p2"]

    tail = plugin_main.resolve_selected_chapters(album, ChapterQuery("tail", 2))
    assert [chapter.pid for chapter in tail] == ["p5", "p6"]

    latest = plugin_main.resolve_selected_chapters(
        album, ChapterQuery("latest", 3)
    )
    assert [chapter.pid for chapter in latest] == ["p4", "p5", "p6"]

    # 超量自动收敛到全部章节
    assert len(
        plugin_main.resolve_selected_chapters(album, ChapterQuery("head", 99))
    ) == 6

    rng = plugin_main.resolve_selected_chapters(
        album, ChapterQuery("range", 2, 3)
    )
    assert [chapter.pid for chapter in rng] == ["p2", "p3", "p4"]

    rng_to_end = plugin_main.resolve_selected_chapters(
        album, ChapterQuery("range", 4, -1)
    )
    assert [chapter.pid for chapter in rng_to_end] == ["p4", "p5", "p6"]

    with pytest.raises(ChapterOutOfRangeError):
        plugin_main.resolve_selected_chapters(album, ChapterQuery("range", 9, 1))


def test_selection_labels_and_names(plugin_main):
    assert "前 2 章" in plugin_main.selection_label(
        ChapterQuery("head", 2), 2, 6
    )
    assert "最新 3 章" in plugin_main.selection_label(
        ChapterQuery("latest", 3), 3, 6
    )
    assert "第 3 章至最新" in plugin_main.selection_label(
        ChapterQuery("range", 3, -1), 4, 6
    )
    assert "第 3-4 章" in plugin_main.selection_label(
        ChapterQuery("range", 3, 2), 2, 6
    )
    assert (
        plugin_main.selection_pdf_name(
            "123", "标题", ChapterQuery("head", 2), 2, 6
        )
        == "JM123-前2章-标题.pdf"
    )
    assert (
        plugin_main.selection_pdf_name(
            "123", "标题", ChapterQuery("range", 3, 2), 2, 6
        )
        == "JM123-第3-4章-标题.pdf"
    )


def test_resolve_pdf_password(plugin_main):
    fixed = PluginConfig(encrypt_pdf=True, pdf_password=" MyPass123 ")
    assert plugin_main.resolve_pdf_password(fixed) == "MyPass123"

    random_password = plugin_main.resolve_pdf_password(
        PluginConfig(encrypt_pdf=True, pdf_password="")
    )
    assert len(random_password) == plugin_main.PDF_PASSWORD_LENGTH

    assert plugin_main.resolve_pdf_password(
        PluginConfig(encrypt_pdf=False, pdf_password="ignored")
    ) == ""


def test_chapter_pdf_name(plugin_main):
    assert (
        plugin_main.chapter_pdf_name("123", "标题", 2, 5, "二章")
        == "JM123-章2of5-二章.pdf"
    )
    assert (
        plugin_main.chapter_pdf_name("123", "标题", 1, 2, "")
        == "JM123-章1of2-标题.pdf"
    )


def test_run_command_dispatches_chapter_and_no_cache(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    calls: list[tuple[str, int | None, bool]] = []

    async def fake_run_job(
        event,
        album_id,
        config,
        job_root,
        chapter_no=None,
        no_cache=False,
        selector=None,
        file_type="pdf",
        preview=None,
    ) -> str:
        calls.append(
            (album_id, chapter_no, no_cache, selector, file_type, preview)
        )
        return "ok"

    plugin._run_job = fake_run_job  # type: ignore[assignment]
    event = FakeEvent()

    assert asyncio.run(plugin._run_command(event, PluginConfig(), "123456")) == "ok"
    assert calls[-1] == ("123456", None, False, None, "pdf", None)
    assert (
        asyncio.run(plugin._run_command(event, PluginConfig(), "123456 3"))
        == "ok"
    )
    assert calls[-1] == ("123456", 3, False, None, "pdf", None)
    assert (
        asyncio.run(
            plugin._run_command(event, PluginConfig(), "--no-cache 123456 2")
        )
        == "ok"
    )
    assert calls[-1] == ("123456", 2, True, None, "pdf", None)
    assert (
        asyncio.run(
            plugin._run_command(event, PluginConfig(), "123456 --head 2")
        )
        == "ok"
    )
    assert calls[-1][:3] == ("123456", None, False)
    assert calls[-1][3] == ChapterQuery("head", 2)
    assert calls[-1][4] == "pdf"
    assert calls[-1][5] is None

    message = asyncio.run(
        plugin._run_command(event, PluginConfig(), "123456 --bad")
    )
    assert "参数错误" in message and "--bad" in message

    message = asyncio.run(plugin._run_command(event, PluginConfig(), "abc"))
    assert "用法" in message


class _FakeSource:
    def __init__(self, album: AlbumInfo) -> None:
        self.album = album
        self.downloaded: list[str] = []

    def fetch_album(self, album_id: str) -> AlbumInfo:
        return self.album

    def fetch_album_cover(self, album_id, save_path):
        from PIL import Image

        save_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 40), color=(9, 8, 7)).save(save_path)
        return save_path

    def preview_pdf(self, pid, images_root, pdf_path, limit):
        from PIL import Image

        from astrbot_jmdownloader.core.pdf import build_chapter_pdf

        images_root.mkdir(parents=True, exist_ok=True)
        images = []
        for index in range(1, min(limit, 3) + 1):
            path = images_root / f"{pid}-{index}.png"
            Image.new("RGB", (20, 30), color=(index, 1, 1)).save(path)
            images.append(path)
        page_count = build_chapter_pdf(images, pdf_path)
        return page_count, pdf_path.stat().st_size

    def download_chapter(self, pid, images_root, pdf_path) -> ChapterArtifact:
        from PIL import Image

        from astrbot_jmdownloader.core.pdf import build_chapter_pdf

        self.downloaded.append(pid)
        images_root.mkdir(parents=True, exist_ok=True)
        image_path = images_root / f"{pid}.png"
        Image.new("RGB", (40, 60), color=(1, 2, 3)).save(image_path)
        build_chapter_pdf([image_path], pdf_path)
        return ChapterArtifact(
            pid=pid,
            page_count=1,
            size_bytes=pdf_path.stat().st_size,
            image_bytes=image_path.stat().st_size,
            pdf_path=pdf_path,
        )


def test_run_job_chapter_mode_uploads_single_chapter(
    plugin_main, monkeypatch, tmp_path
):
    album = _album()
    source = _FakeSource(album)
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: source)

    plugin = plugin_main.JmDownloaderPlugin(
        None, {"encrypt_pdf": False, "cache_albums": 0}
    )
    uploads: list[str] = []

    async def fake_upload(event, pdf_path, file_name, config):
        uploads.append(file_name)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    event = FakeEvent()
    job_root = tmp_path / "job"
    job_root.mkdir()

    text = asyncio.run(
        plugin._run_job(
            event, "999", PluginConfig.from_mapping(plugin.raw_config), job_root, 2
        )
    )
    assert "第 2 章" in text
    assert "chapter-2" in text
    assert uploads == ["JM999-章2of3-chapter-2.pdf"]
    assert source.downloaded == ["p2"]  # 只下载选中的一章


def test_run_job_archive_zip_upload(plugin_main, monkeypatch, tmp_path):
    import zipfile

    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"encrypt_pdf": False, "cache_albums": 0}
    )
    uploads: list[tuple[str, Path]] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append((file_name, path))
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "archive-job"
    job_root.mkdir()
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            file_type="zip",
        )
    )
    assert "zip 压缩包已作为 QQ 文件上传" in text
    assert len(uploads) == 1
    file_name, archive_path = uploads[0]
    assert file_name == "JM999-测试本.zip"
    assert archive_path.suffix == ".zip"
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    assert names == ["001-chapter-1.pdf", "002-chapter-2.pdf", "003-chapter-3.pdf"]


def test_run_job_archive_zip_with_password(
    plugin_main, monkeypatch, tmp_path
):
    pyzipper = pytest.importorskip("pyzipper")
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None,
        {
            "encrypt_pdf": True,
            "pdf_password": "ZipPass123",
            "cache_albums": 0,
        },
    )
    uploads: list[Path] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append(path)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "archive-pass"
    job_root.mkdir()
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            file_type="zip",
        )
    )
    assert "打开密码：ZipPass123" in text
    assert len(uploads) == 1
    with pyzipper.AESZipFile(uploads[0]) as archive:
        archive.setpassword(b"ZipPass123")
        assert archive.namelist() == [
            "001-chapter-1.pdf",
            "002-chapter-2.pdf",
            "003-chapter-3.pdf",
        ]


def test_run_job_archive_tar_refuses_password(
    plugin_main, monkeypatch, tmp_path
):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None,
        {
            "encrypt_pdf": True,
            "pdf_password": "TarPass123",
            "cache_albums": 0,
        },
    )
    uploads: list[str] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append(file_name)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "archive-tar-pass"
    job_root.mkdir()
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            file_type="tar.gz",
        )
    )
    assert "tar" in text and "不支持" in text
    assert uploads == []


def test_run_job_preview_pdf_default_type(
    plugin_main, monkeypatch, tmp_path
):
    from astrbot_jmdownloader.core.pdf import pdf_page_count

    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"encrypt_pdf": False, "cache_albums": 0}
    )
    uploads: list[tuple[str, Path]] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append((file_name, path))
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "preview-job"
    job_root.mkdir()
    event = FakeEvent()
    text = asyncio.run(
        plugin._run_job(
            event,
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            chapter_no=2,
            preview=2,
        )
    )
    assert "前 2 页，pdf" in text
    assert len(uploads) == 1
    file_name, uploaded = uploads[0]
    assert "预览2页" in file_name and file_name.endswith(".pdf")
    assert pdf_page_count(uploaded) == 2
    assert not any(chain.images for chain in event.chains)


def test_run_job_preview_respects_configured_type(
    plugin_main, monkeypatch, tmp_path
):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"encrypt_pdf": False, "cache_albums": 0}
    )
    uploads: list[tuple[str, Path]] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append((file_name, path))
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "preview-tar"
    job_root.mkdir()
    configured = PluginConfig.from_mapping(
        {"basic": {"default_file_type": "tar.gz"}, "encrypt_pdf": False}
    )
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            configured,
            job_root,
            chapter_no=1,
            preview=2,
            file_type="tar.gz",
        )
    )
    assert "tar.gz" in text
    file_name, uploaded = uploads[0]
    assert file_name.endswith(".tar.gz")
    with tarfile.open(uploaded, "r:gz") as archive:
        names = archive.getnames()
    assert len(names) == 1 and names[0].endswith(".pdf")


def test_run_job_preview_zip_with_password(
    plugin_main, monkeypatch, tmp_path
):
    pyzipper = pytest.importorskip("pyzipper")
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"cache_albums": 0}
    )
    uploads: list[Path] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append(path)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "preview-zip"
    job_root.mkdir()
    configured = PluginConfig.from_mapping(
        {
            "basic": {"encrypt_pdf": True, "pdf_password": "PrePass123"},
            "cache": {"cache_albums": 0},
        }
    )
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            configured,
            job_root,
            chapter_no=1,
            preview=2,
            file_type="zip",
        )
    )
    assert "打开密码" not in text  # 预览消息本身不含密码；密码由统一发送逻辑处理
    assert "已加密" in text
    with pyzipper.AESZipFile(uploads[0]) as archive:
        archive.setpassword(b"PrePass123")
        assert len(archive.namelist()) == 1


def test_run_job_preview_tar_refuses_password(
    plugin_main, monkeypatch, tmp_path
):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    uploads: list[str] = []

    async def fake_upload(event, path, file_name, config):
        uploads.append(file_name)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "preview-tar-pass"
    job_root.mkdir()
    configured = PluginConfig.from_mapping(
        {"basic": {"encrypt_pdf": True, "pdf_password": "x"}}
    )
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            configured,
            job_root,
            preview=2,
            file_type="tar.gz",
        )
    )
    assert "tar" in text and "不支持" in text
    assert uploads == []


def test_run_job_chapter_out_of_range(plugin_main, monkeypatch, tmp_path):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {"cache_albums": 0})
    text = asyncio.run(
        plugin._run_job(
            event=FakeEvent(),
            album_id="999",
            config=PluginConfig(),
            job_root=tmp_path,
            chapter_no=9,
        )
    )
    assert "共 3 章" in text


def test_save_cache_merge_existing_keeps_other_chapters(plugin_main, tmp_path):
    album = _album()
    cache = ChapterCache(tmp_path / "cache")
    chapters_dir = cache.chapters_dir(album.album_id)
    for index in (1, 2):
        (chapters_dir / f"p{index}.pdf").write_bytes(b"%PDF-1.4 " + bytes([index]))
    cache.save_album(
        album.album_id,
        album.title,
        [
            plugin_main.CachedChapter("p1", "1", "chapter-1", 1, 0),
            plugin_main.CachedChapter("p2", "2", "chapter-2", 1, 0),
        ],
    )

    plugin = plugin_main.JmDownloaderPlugin(None, {})
    updated = [
        ChapterPdf("p2", "2", "chapter-2", 2, 99, chapters_dir / "p2.pdf")
    ]
    plugin._save_cache(
        cache,
        album,
        updated,
        PluginConfig(),
        plugin_main.JobStats(downloaded_chapters=1),
        merge_existing=True,
    )
    loaded = cache.load_chapters(album.album_id)
    assert set(loaded) == {"p1", "p2"}  # p1 未被单章刷新覆盖
    assert loaded["p2"].page_count == 2


def test_collect_chapters_ignore_cache_redownloads(plugin_main, tmp_path):
    album = _album()
    cache = ChapterCache(tmp_path / "cache")
    source = _FakeSource(album)
    plugin = plugin_main.JmDownloaderPlugin(None, {"cache_albums": 5})

    # 先构建一份缓存（p1），再让 ignore_cache=True 强制重新下载
    chapters_dir = cache.chapters_dir(album.album_id)
    for index in (1, 2):
        (chapters_dir / f"p{index}.pdf").write_bytes(b"%PDF-1.4 " + bytes([index]))
    cache.save_album(
        album.album_id,
        album.title,
        [
            plugin_main.CachedChapter("p1", "1", "chapter-1", 1, 0),
            plugin_main.CachedChapter("p2", "2", "chapter-2", 1, 0),
        ],
    )
    job_root = tmp_path / "job"
    job_root.mkdir()
    chapters, stats = asyncio.run(
        plugin._collect_chapters(
            source,
            album,
            PluginConfig(),
            job_root,
            cache,
            (album.chapters[0],),
            ignore_cache=True,
        )
    )
    assert source.downloaded == ["p1"]
    assert stats.reused_chapters == 0
    assert stats.downloaded_chapters == 1
    assert chapters[0].pid == "p1"


def test_help_commands(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()
    for args in ("", "help", "--help", "-h", "HELP", "help me"):
        text = asyncio.run(plugin._run_command(event, PluginConfig(), args))
        assert "/jm <漫画ID>" in text
        assert "--no-cache" in text
        assert "--range" in text


def test_run_job_head_selection_merges_pdf(plugin_main, monkeypatch, tmp_path):
    album = _album()
    source = _FakeSource(album)
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: source)
    plugin = plugin_main.JmDownloaderPlugin(
        None, {"encrypt_pdf": False, "cache_albums": 0}
    )
    uploads: list[str] = []

    async def fake_upload_once(event, pdf_path, file_name, config):
        uploads.append(file_name)
        return {}

    monkeypatch.setattr(plugin, "_upload_once", fake_upload_once)
    job_root = tmp_path / "job"
    job_root.mkdir()

    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            None,
            False,
            ChapterQuery("head", 2),
        )
    )
    assert "前 2 章" in text
    assert uploads == ["JM999-前2章-测试本.pdf"]
    # 多章并发下载的完成顺序不保证，按集合比较
    assert sorted(source.downloaded) == ["p1", "p2"]


def test_run_job_range_out_of_bounds(plugin_main, monkeypatch, tmp_path):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(None, {"cache_albums": 0})
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig(),
            tmp_path,
            None,
            False,
            ChapterQuery("range", 9, 1),
        )
    )
    assert "起始章节应在" in text


def test_run_job_uses_fixed_password(plugin_main, monkeypatch, tmp_path):
    album = _album()
    monkeypatch.setattr(plugin_main, "JmSource", lambda config: _FakeSource(album))
    plugin = plugin_main.JmDownloaderPlugin(
        None,
        {
            "encrypt_pdf": True,
            "pdf_password": "MyPass123",
            "cache_albums": 0,
        },
    )
    uploads: list[str] = []

    async def fake_upload(event, pdf_path, file_name, config):
        uploads.append(file_name)
        return {}

    monkeypatch.setattr(plugin, "_upload_with_retries", fake_upload)
    job_root = tmp_path / "job"
    job_root.mkdir()
    text = asyncio.run(
        plugin._run_job(
            FakeEvent(),
            "999",
            PluginConfig.from_mapping(plugin.raw_config),
            job_root,
            1,
            False,
            None,
        )
    )
    assert "打开密码：MyPass123" in text
    assert uploads == ["JM999-章1of3-chapter-1.pdf"]


def test_is_help_request(plugin_main):
    assert plugin_main.is_help_request("") is True
    assert plugin_main.is_help_request("help") is True
    assert plugin_main.is_help_request("--help") is True
    assert plugin_main.is_help_request("-h") is True
    assert plugin_main.is_help_request("HELP") is True
    assert plugin_main.is_help_request("123456") is False
    assert plugin_main.is_help_request("123456 --head 2") is False


def test_send_help_sends_image(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()

    async def fake_text_to_image(text, return_url=True, umo=None):
        assert text is plugin_main.HELP_TEXT
        return "/tmp/help.jpg"

    plugin.text_to_image = fake_text_to_image  # type: ignore[assignment]
    asyncio.run(plugin._send_help(event))
    assert event.chains[-1].images == [("file", "/tmp/help.jpg")]
    assert event.messages[-1] == ""


def test_send_help_uses_url_image(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()

    async def fake_text_to_image(text, return_url=True, umo=None):
        return "http://astrbot:6185/api/file/help.jpg"

    plugin.text_to_image = fake_text_to_image  # type: ignore[assignment]
    asyncio.run(plugin._send_help(event))
    assert event.chains[-1].images == [
        ("url", "http://astrbot:6185/api/file/help.jpg")
    ]


def test_send_help_falls_back_to_text(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()

    async def broken_text_to_image(text, return_url=True, umo=None):
        raise RuntimeError("t2i unavailable")

    plugin.text_to_image = broken_text_to_image  # type: ignore[assignment]
    asyncio.run(plugin._send_help(event))
    assert event.chains[-1].images == []
    assert event.messages[-1] == plugin_main.HELP_TEXT


def test_handler_help_sends_image(plugin_main):
    plugin = plugin_main.JmDownloaderPlugin(None, {})
    event = FakeEvent()

    async def fake_text_to_image(text, return_url=True, umo=None):
        return "/tmp/help.jpg"

    plugin.text_to_image = fake_text_to_image  # type: ignore[assignment]
    asyncio.run(plugin.download_command(event, "help"))
    assert event.stopped is True
    assert event.chains[-1].images == [("file", "/tmp/help.jpg")]
