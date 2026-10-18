"""测试夹具：安装 AstrBot 桩模块，把插件仓库加入导入路径。

单测不依赖 AstrBot 运行环境：``main`` 需要的 ``astrbot.api`` 由桩模块提供，
PDF 相关用例使用真实的 ``img2pdf`` / ``pypdf``，分卷规划与缓存为纯逻辑。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

# 插件在 AstrBot 里以 ``data.plugins.<name>`` 命名空间包加载；为了让测试
# 不依赖仓库目录名（本地可能是 astrbot_jmdownloader，容器里可能是临时目录名），
# 这里把插件根目录注册成固定包名 ``astrbot_jmdownloader``。
_PACKAGE_NAME = "astrbot_jmdownloader"
if _PACKAGE_NAME not in sys.modules:
    _package = types.ModuleType(_PACKAGE_NAME)
    _package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[_PACKAGE_NAME] = _package


class StubMessageChain:
    """AstrBot MessageChain 的最小桩。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.images: list[tuple[str, str]] = []

    @classmethod
    def message(cls, text: str) -> "StubMessageChain":
        return cls(text)

    def url_image(self, url: str) -> "StubMessageChain":
        self.images.append(("url", url))
        return self

    def file_image(self, path: str) -> "StubMessageChain":
        self.images.append(("file", path))
        return self


class _StubFilter:
    def command(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def __getattr__(self, item):
        def decorator(*args, **kwargs):
            def wrapper(func):
                return func

            return wrapper

        return decorator


class StubStar:
    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.logger = logging.getLogger("test.astrbot_jmdownloader")


class StubTokenService:
    """``astrbot.core.file_token_service`` 的桩，可在用例里改行为。"""

    def __init__(self) -> None:
        self.token = "tok-123"
        self.error: Exception | None = None
        self.calls: list[dict] = []

    async def register_file(self, path, timeout=None, single_use=True):
        self.calls.append(
            {"path": path, "timeout": timeout, "single_use": single_use}
        )
        if self.error is not None:
            raise self.error
        return self.token


def install_astrbot_stubs() -> None:
    """安装最小 AstrBot 模块树；已安装时不覆盖（便于用例 monkeypatch）。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test.astrbot")

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api_event.MessageChain = StubMessageChain
    api_event.filter = _StubFilter()

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = type("Context", (), {})
    api_star.Star = StubStar

    core = types.ModuleType("astrbot.core")
    core.astrbot_config = {"callback_api_base": ""}
    core.file_token_service = StubTokenService()

    astrbot.api = api
    astrbot.core = core
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.api.star": api_star,
            "astrbot.core": core,
        }
    )


install_astrbot_stubs()


@pytest.fixture()
def astrbot_core() -> types.ModuleType:
    """返回可修改的 ``astrbot.core`` 桩模块。"""
    return sys.modules["astrbot.core"]


@pytest.fixture()
def stub_token_service(astrbot_core: types.ModuleType) -> StubTokenService:
    """安装并返回文件服务桩，用例可修改 token/error。"""
    service = StubTokenService()
    astrbot_core.file_token_service = service
    return service


@pytest.fixture()
def plugin_main():
    """导入插件入口模块。"""
    import importlib

    return importlib.import_module("astrbot_jmdownloader.main")
