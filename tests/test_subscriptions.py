"""订阅调度、存储与解析测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from astrbot_jmdownloader.core.subscriptions import (
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    build_target,
    cron_next,
    next_run,
    parse_loop_interval,
    validate_cron,
)


def test_parse_loop_interval():
    assert parse_loop_interval("60s") == 60
    assert parse_loop_interval("5m") == 300
    assert parse_loop_interval("6h") == 21600
    assert parse_loop_interval("1d") == 86400
    with pytest.raises(SubscriptionError):
        parse_loop_interval("30s")
    with pytest.raises(SubscriptionError):
        parse_loop_interval("abc")


def test_cron_validation_and_next():
    assert validate_cron("0 9 * * *") == "0 9 * * *"
    with pytest.raises(SubscriptionError):
        validate_cron("0 9 * *")
    base = datetime(2026, 10, 18, 10, 30)
    assert cron_next("0 9 * * *", base) == datetime(2026, 10, 19, 9, 0)
    assert cron_next("*/15 * * * *", base) == datetime(2026, 10, 18, 10, 45)
    assert next_run("loop", "2h", base) == base + timedelta(hours=2)


def test_store_roundtrip(tmp_path):
    store = SubscriptionStore(tmp_path)
    sub = Subscription(
        album_id="123",
        album_title="标题",
        schedule_kind="loop",
        schedule_value="6h",
        targets=["aiocqhttp:GroupMessage:111"],
    )
    store.upsert(sub)
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].album_id == "123"
    assert loaded[0].targets == ["aiocqhttp:GroupMessage:111"]
    store.save_defaults({"schedule_kind": "cron"})
    assert store.load_defaults()["schedule_kind"] == "cron"
    assert store.delete("123") is True
    assert store.load() == []


def test_build_target():
    class Event:
        unified_msg_origin = "aiocqhttp:GroupMessage:999"

    assert build_target(Event(), "group") == ["aiocqhttp:GroupMessage:999"]
    assert build_target(Event(), "group", "111,222") == [
        "aiocqhttp:GroupMessage:111",
        "aiocqhttp:GroupMessage:222",
    ]
    assert build_target(Event(), "user", "333") == [
        "aiocqhttp:FriendMessage:333"
    ]
