"""动态分卷规划测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_jmdownloader.core.errors import SplitNotPossibleError
from astrbot_jmdownloader.core.models import ChapterPdf, DeliveryMode, MIB
from astrbot_jmdownloader.core.planner import (
    BASE64_HARD_LIMIT_BYTES,
    URL_HARD_LIMIT_BYTES,
    SplitInput,
    plan_split,
)


def _chapters(sizes_mib: list[float]) -> list[ChapterPdf]:
    return [
        ChapterPdf(
            pid=str(index),
            index=str(index),
            title=f"chapter-{index}",
            page_count=10,
            size_bytes=int(size * MIB),
            path=Path(f"chapter-{index}.pdf"),
        )
        for index, size in enumerate(sizes_mib)
    ]


def _input(
    sizes_mib: list[float],
    *,
    whole_mib: float | None = None,
    delivery: DeliveryMode = DeliveryMode.FILE_URL,
    downloaded_mib: float = 0,
    manual_target_mb: int = 0,
    manual_max_volumes: int = 0,
) -> SplitInput:
    whole = sum(sizes_mib) if whole_mib is None else whole_mib
    return SplitInput(
        chapters=tuple(_chapters(sizes_mib)),
        whole_bytes=int(whole * MIB),
        downloaded_bytes=int(downloaded_mib * MIB),
        delivery=delivery,
        manual_target_mb=manual_target_mb,
        manual_max_volumes=manual_max_volumes,
    )


def test_single_chapter_cannot_split():
    with pytest.raises(SplitNotPossibleError):
        plan_split(_input([100]))


def test_auto_keeps_chapter_boundaries_and_order():
    plan = plan_split(_input([20] * 12))
    flattened = [
        chapter.pid for volume in plan.volumes for chapter in volume.chapters
    ]
    assert flattened == [str(index) for index in range(12)]
    assert plan.volume_count >= 2
    assert sum(volume.chapter_count for volume in plan.volumes) == 12


def test_auto_picks_minimal_volume_count_meeting_soft_target():
    # 10 章 × 30 MiB = 300 MiB，URL 软目标 100 MiB：
    # 3 卷最优也只能到 120 MiB，4 卷可做到 90 MiB，因此应选 4 卷。
    plan = plan_split(_input([30] * 10))
    assert plan.volume_count == 4
    assert max(volume.estimated_bytes for volume in plan.volumes) <= 100 * MIB


def test_auto_balances_volumes():
    plan = plan_split(_input([60] * 5, whole_mib=300))
    sizes = [volume.estimated_bytes for volume in plan.volumes]
    # 每章 60 MiB，软目标 100 MiB：只能一章一卷，5 卷各 60 MiB
    assert plan.volume_count == 5
    assert max(sizes) - min(sizes) == 0


def test_auto_respects_hard_limit_when_exceeded():
    plan = plan_split(_input([500, 60]))
    # 单章 500 MiB 本身超过硬上限，分卷也无法压下来，但方案必须标记风险
    assert plan.exceeds_hard_limit is True
    assert plan.hard_limit_bytes == URL_HARD_LIMIT_BYTES


def test_base64_delivery_uses_larger_hard_limit():
    plan = plan_split(
        _input(
            [120] * 6,
            whole_mib=720,
            delivery=DeliveryMode.BASE64,
        )
    )
    assert plan.hard_limit_bytes == BASE64_HARD_LIMIT_BYTES
    assert plan.volume_count >= 2


def test_manual_override_respects_max_volumes():
    # 10 章 × 60 MiB = 600 MiB；手动目标 100 MiB 本应 6 卷，上限 3 卷
    plan = plan_split(
        _input([60] * 10, manual_target_mb=100, manual_max_volumes=3)
    )
    assert plan.volume_count == 3
    assert plan.mode == "manual"
    assert plan.volume_count <= 3


def test_manual_override_zero_target_not_used_in_manual_mode():
    # manual_target_mb=0 表示不进入手动分支（由上层决定是否允许回退）
    plan = plan_split(_input([60] * 4, manual_target_mb=0))
    assert plan.mode == "auto"


def test_scale_calibrated_by_whole_pdf_size():
    # 章节 PDF 合计 50 MiB，整本加密后 75 MiB → 体积系数 1.5
    plan = plan_split(_input([10] * 5, whole_mib=75))
    assert sum(
        volume.estimated_bytes for volume in plan.volumes
    ) == pytest.approx(75 * MIB)
    assert "体积系数 1.50" in plan.basis


def test_basis_contains_downloaded_bytes():
    plan = plan_split(_input([20] * 4, downloaded_mib=70))
    assert "图片下载 70.0 MiB" in plan.basis


def test_plan_is_deterministic():
    first = plan_split(_input([13, 27, 41, 5, 62, 18, 9, 33]))
    second = plan_split(_input([13, 27, 41, 5, 62, 18, 9, 33]))
    assert first == second


def test_all_volumes_non_empty_and_cover_all_chapters():
    plan = plan_split(_input([3] * 40))
    assert all(volume.chapters for volume in plan.volumes)
    flattened = [
        chapter.pid for volume in plan.volumes for chapter in volume.chapters
    ]
    assert flattened == [str(index) for index in range(40)]
    assert plan.target_bytes == max(
        volume.estimated_bytes for volume in plan.volumes
    )


def test_min_volume_count_replans_more_volumes():
    base = plan_split(_input([40] * 10))
    replanned = plan_split(
        SplitInput(
            chapters=tuple(_chapters([40] * 10)),
            whole_bytes=400 * MIB,
            delivery=DeliveryMode.FILE_URL,
            min_volume_count=base.volume_count + 1,
        )
    )
    assert replanned.volume_count > base.volume_count
