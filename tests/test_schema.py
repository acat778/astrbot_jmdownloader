"""_conf_schema.json 结构测试：卡片分组、条件显示与固定密码项。"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_top_level_groups_are_object_cards():
    schema = _schema()
    assert list(schema) == [
        "basic",
        "groups",
        "subscriptions",
        "split",
        "upload",
        "cache",
        "network",
    ]
    for group in schema.values():
        assert group["type"] == "object"
        assert group["description"]
        assert isinstance(group["items"], dict) and group["items"]


def test_split_auto_switch_and_condition_in_same_card():
    split = _schema()["split"]["items"]
    assert split["split_auto"]["type"] == "bool"
    assert split["split_auto"]["default"] is True
    assert split["split_size_mb"]["condition"] == {"split_auto": False}
    assert split["split_max_volumes"]["condition"] == {"split_auto": False}


def test_fixed_pdf_password_item():
    basic = _schema()["basic"]["items"]
    assert basic["pdf_password"]["type"] == "string"
    assert basic["pdf_password"]["default"] == ""
    assert basic["pdf_password"]["condition"] == {"encrypt_pdf": True}


def test_removed_legacy_flat_keys():
    schema = _schema()
    flat_keys = set()
    for group in schema.values():
        flat_keys.update(group["items"])
    assert "enabled" not in schema
    assert flat_keys == {
        "enabled",
        "allow_group",
        "group_whitelist",
        "group_blacklist",
        "encrypt_pdf",
        "pdf_password",
        "split_auto",
        "split_size_mb",
        "split_max_volumes",
        "callback_api_base",
        "upload_retries",
        "cache_albums",
        "preview_pages",
        "default_file_type",
        "sub_default_schedule_kind",
        "sub_default_schedule_value",
        "sub_default_type",
        "sub_default_push_mode",
        "sub_default_notice_type",
        "sub_default_notice_sids",
        "sub_default_notice_timing",
        "sub_default_notice_cron",
        "sub_check_interval",
        "cookies",
        "proxy",
        "timeout",
        "retry_times",
        "image_threads",
        "photo_threads",
        "max_concurrent",
    }
