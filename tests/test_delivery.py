"""文件投递（URL / base64 回退）测试。"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from astrbot_jmdownloader.core import delivery
from astrbot_jmdownloader.core.errors import UploadConfigError


class FakeApi:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("protocol upload failed")
        return {"file_id": "ok"}


class FakeBot:
    def __init__(self, failures: int = 0) -> None:
        self.api = FakeApi(failures)


class FakeEvent:
    def __init__(self, group_id: str = "", user_id: str = "75782383") -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.bot = FakeBot()

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id


def _pdf(tmp_path: Path, payload: bytes = b"%PDF-1.4" + b"x" * 64) -> Path:
    path = tmp_path / "sample.pdf"
    path.write_bytes(payload)
    return path


def test_resolve_callback_base_prefers_plugin_value(astrbot_core):
    astrbot_core.astrbot_config = {"callback_api_base": "http://global:6185"}
    assert delivery.resolve_callback_base("http://plugin:6185") == (
        "http://plugin:6185"
    )
    assert delivery.resolve_callback_base("") == "http://global:6185"


def test_resolve_callback_base_empty(astrbot_core):
    astrbot_core.astrbot_config = {"callback_api_base": ""}
    assert delivery.resolve_callback_base("") == ""


def test_build_upload_uri_uses_file_token(astrbot_core, stub_token_service, tmp_path):
    pdf = _pdf(tmp_path)

    uri = asyncio.run(delivery.build_upload_uri(pdf, "http://astrbot:6185/"))
    assert uri == "http://astrbot:6185/api/file/tok-123"
    assert stub_token_service.calls[0]["timeout"] == delivery.FILE_URL_TTL_SECONDS
    assert stub_token_service.calls[0]["single_use"] is False


def test_build_upload_uri_falls_back_to_base64_when_no_base(
    astrbot_core, tmp_path
):
    pdf = _pdf(tmp_path, b"%PDF-1.4 fallback")
    uri = asyncio.run(delivery.build_upload_uri(pdf, ""))
    assert uri.startswith("base64://")
    assert base64.b64decode(uri.removeprefix("base64://")) == pdf.read_bytes()


def test_build_upload_uri_falls_back_when_token_registration_fails(
    astrbot_core, stub_token_service, tmp_path
):
    stub_token_service.error = RuntimeError("token service down")
    pdf = _pdf(tmp_path)

    uri = asyncio.run(delivery.build_upload_uri(pdf, "http://astrbot:6185"))
    assert uri.startswith("base64://")


def test_base64_budget_rejects_large_file(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery, "MAX_BASE64_BYTES", 8)
    pdf = _pdf(tmp_path, b"%PDF-1.4" + b"x" * 100)
    with pytest.raises(UploadConfigError):
        delivery.base64_file_uri(pdf)


def test_upload_pdf_falls_back_to_base64_after_url_failure(
    astrbot_core, stub_token_service, tmp_path
):
    pdf = _pdf(tmp_path)
    event = FakeEvent()
    event.bot = FakeBot(failures=1)

    asyncio.run(delivery.upload_pdf(event, pdf, "JM1.pdf", "http://astrbot:6185"))
    assert len(event.bot.api.calls) == 2
    first_action, first_kwargs = event.bot.api.calls[0]
    second_action, second_kwargs = event.bot.api.calls[1]
    assert first_action == "upload_private_file"
    assert first_kwargs["file"].startswith("http://")
    assert second_kwargs["file"].startswith("base64://")
    assert second_action == "upload_private_file"


def test_upload_pdf_uses_group_action(astrbot_core, stub_token_service, tmp_path):
    pdf = _pdf(tmp_path)
    event = FakeEvent(group_id="123456")

    asyncio.run(delivery.upload_pdf(event, pdf, "JM1.pdf", "http://astrbot:6185"))
    action, kwargs = event.bot.api.calls[0]
    assert action == "upload_group_file"
    assert kwargs["group_id"] == 123456
