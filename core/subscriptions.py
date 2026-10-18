"""漫画订阅的持久化、调度时间计算与目标会话构造。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SUBSCRIPTION_FILE_NAME = "subscriptions.json"
DEFAULT_SCHEDULE_KIND = "loop"
DEFAULT_SCHEDULE_VALUE = "6h"
DEFAULT_FILE_TYPE = "pdf"
DEFAULT_PUSH_MODE = "file"
DEFAULT_NOTICE_TYPE = "group"
DEFAULT_NOTICE_TIMING = "realtime"
DEFAULT_NOTICE_CRON = "0 9 * * *"

MIN_LOOP_SECONDS = 60
MAX_LOOP_SECONDS = 30 * 24 * 3600
_LOOP_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_LOOP_RE = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)


def resolve_subscription_root(plugin_name: str) -> Path | None:
    """返回插件数据目录；不可用时返回 None。"""
    try:
        from astrbot.core.star.star_tools import StarTools

        return Path(StarTools.get_data_dir(plugin_name))
    except Exception:  # noqa: BLE001 - 兼容不同版本/测试环境
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            data.mkdir(parents=True, exist_ok=True)
            return data
        except Exception:  # noqa: BLE001
            return None


class SubscriptionError(ValueError):
    """订阅参数错误。"""


def parse_loop_interval(raw: Any) -> int:
    """把 ``30s``/``5m``/``6h``/``2d`` 或纯数字（秒）换算成秒。"""
    text = str(raw or "").strip()
    matched = _LOOP_RE.fullmatch(text)
    if not matched:
        raise SubscriptionError(
            f"loop 间隔格式不正确：{raw or '(空)'}；示例：30m、6h、1d"
        )
    seconds = int(matched.group(1)) * _LOOP_UNITS[(matched.group(2) or "s").lower()]
    if seconds < MIN_LOOP_SECONDS:
        raise SubscriptionError(f"loop 间隔不能小于 {MIN_LOOP_SECONDS} 秒")
    if seconds > MAX_LOOP_SECONDS:
        raise SubscriptionError("loop 间隔不能超过 30 天")
    return seconds


def _cron_field_values(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for chunk in field.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise SubscriptionError(f"cron 字段为空：{field}")
        step = 1
        if "/" in chunk:
            chunk, step_text = chunk.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                raise SubscriptionError(f"cron 步长不正确：{field}")
            step = int(step_text)
        if chunk == "*":
            start, end = low, high
        elif "-" in chunk:
            start_text, end_text = chunk.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise SubscriptionError(f"cron 范围不正确：{field}")
            start, end = int(start_text), int(end_text)
        elif chunk.isdigit():
            start = end = int(chunk)
        else:
            raise SubscriptionError(f"cron 字段不正确：{field}")
        if start < low or end > high or start > end:
            raise SubscriptionError(f"cron 字段超出范围：{field}（{low}-{high}）")
        values.update(range(start, end + 1, step))
    return values


def validate_cron(expression: str) -> str:
    """校验 5 段 cron 表达式（分 时 日 月 周），返回规范化字符串。"""
    parts = str(expression or "").strip().split()
    if len(parts) != 5:
        raise SubscriptionError("cron 表达式需要 5 段：分 时 日 月 周")
    _cron_field_values(parts[0], 0, 59)
    _cron_field_values(parts[1], 0, 23)
    _cron_field_values(parts[2], 1, 31)
    _cron_field_values(parts[3], 1, 12)
    days = _cron_field_values(parts[4], 0, 7)
    if 7 in days:
        days.discard(7)
        days.add(0)
    return " ".join(parts)


def cron_next(expression: str, after: datetime) -> datetime:
    """计算 ``after`` 之后的下一个 cron 触发时间（按本地时区）。"""
    parts = validate_cron(expression).split()
    minutes = sorted(_cron_field_values(parts[0], 0, 59))
    hours = sorted(_cron_field_values(parts[1], 0, 23))
    days = _cron_field_values(parts[2], 1, 31)
    months = _cron_field_values(parts[3], 1, 12)
    weekdays = _cron_field_values(parts[4], 0, 7)
    weekdays = {0 if day == 7 else day for day in weekdays}
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for _ in range(366 * 5):
        # 标准 cron：日字段与周字段同时受限时取“或”
        day_ok = day.day in days and day.month in months
        weekday_ok = ((day.weekday() + 1) % 7) in weekdays
        if day_ok or weekday_ok:
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(
                        day.year, day.month, day.day, hour, minute
                    )
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    raise SubscriptionError("无法计算 cron 的下一次触发时间（5 年内无匹配）")


def next_run(kind: str, value: str, after: datetime) -> datetime:
    """按订阅方式计算下一次轮询/推送时间。"""
    if kind == "loop":
        return after + timedelta(seconds=parse_loop_interval(value))
    if kind == "cron":
        return cron_next(validate_cron(value), after)
    raise SubscriptionError(f"不支持的订阅方式：{kind}")


@dataclass
class Subscription:
    """一个本子的更新订阅。"""

    album_id: str
    album_title: str = ""
    schedule_kind: str = DEFAULT_SCHEDULE_KIND
    schedule_value: str = DEFAULT_SCHEDULE_VALUE
    file_type: str = DEFAULT_FILE_TYPE
    push_mode: str = DEFAULT_PUSH_MODE
    notice_type: str = DEFAULT_NOTICE_TYPE
    targets: list[str] = field(default_factory=list)
    notice_timing: str = DEFAULT_NOTICE_TIMING
    notice_cron: str = ""
    seen_pids: list[str] = field(default_factory=list)
    pending_pids: list[str] = field(default_factory=list)
    last_check_at: str = ""
    next_run_at: str = ""
    next_notice_at: str = ""
    created_at: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Subscription":
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def next_run(self) -> datetime | None:
        return _parse_dt(self.next_run_at)

    @property
    def next_notice(self) -> datetime | None:
        return _parse_dt(self.next_notice_at)


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SubscriptionStore:
    """订阅与默认值的 JSON 持久化（原子写）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / SUBSCRIPTION_FILE_NAME

    def _load_raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"subscriptions": [], "defaults": {}}
        if not isinstance(data, dict):
            return {"subscriptions": [], "defaults": {}}
        return data

    def _save_raw(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(self.path)

    def load(self) -> list[Subscription]:
        data = self._load_raw()
        items = data.get("subscriptions")
        if not isinstance(items, list):
            return []
        return [
            Subscription.from_dict(item)
            for item in items
            if isinstance(item, dict) and item.get("album_id")
        ]

    def save(self, subscriptions: list[Subscription]) -> None:
        data = self._load_raw()
        data["subscriptions"] = [item.to_dict() for item in subscriptions]
        self._save_raw(data)

    def load_defaults(self) -> dict[str, Any]:
        defaults = self._load_raw().get("defaults")
        return defaults if isinstance(defaults, dict) else {}

    def save_defaults(self, defaults: dict[str, Any]) -> None:
        data = self._load_raw()
        data["defaults"] = dict(defaults)
        self._save_raw(data)

    def get(self, album_id: str) -> Subscription | None:
        for item in self.load():
            if item.album_id == album_id:
                return item
        return None

    def upsert(self, subscription: Subscription) -> None:
        items = [item for item in self.load() if item.album_id != subscription.album_id]
        items.append(subscription)
        self.save(items)

    def delete(self, album_id: str) -> bool:
        items = self.load()
        remaining = [item for item in items if item.album_id != album_id]
        if len(remaining) == len(items):
            return False
        self.save(remaining)
        return True


# ---- 目标会话 -------------------------------------------------------------

def split_sids(raw: str) -> list[str]:
    """把 ``123,456`` 或换行分隔的会话 ID 拆开。"""
    return [part.strip() for part in re.split(r"[,，;；\s]+", str(raw or "")) if part.strip()]


def build_target(
    event: Any, notice_type: str, sids: str | list[str] | None = None
) -> list[str]:
    """把通知目标转成 AstrBot 的 unified_msg_origin 列表。

    当前会话直接复用 ``event.unified_msg_origin``；显式 sid 时按事件平台
    构造 ``<platform>:GroupMessage:<sid>`` 或 ``<platform>:FriendMessage:<sid>``。
    """
    sid_list = sids if isinstance(sids, list) else split_sids(sids or "")
    if not sid_list:
        return [str(getattr(event, "unified_msg_origin", "")).strip()] if getattr(
            event, "unified_msg_origin", ""
        ) else []
    if notice_type not in {"group", "user"}:
        raise SubscriptionError("通知目标只能是 group 或 user")
    platform_name = str(getattr(event, "unified_msg_origin", "")).split(":", 1)[0]
    if not platform_name:
        raise SubscriptionError("无法识别当前会话的平台，无法构造通知目标")
    message_type = "GroupMessage" if notice_type == "group" else "FriendMessage"
    return [f"{platform_name}:{message_type}:{sid}" for sid in sid_list]


def target_kind(umo: str) -> str:
    """从 unified_msg_origin 推断群聊/私聊。"""
    parts = str(umo or "").split(":", 2)
    return "group" if len(parts) >= 2 and parts[1] == "GroupMessage" else "user"
