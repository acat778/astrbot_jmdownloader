"""按实测体积动态规划分卷。

整本 PDF 上传失败后，需要把同一本漫画按「章节边界」切成若干卷重新上传。
单卷目标与卷数不是固定配置，而是由本模块根据以下实测数据动态计算：

* ``whole_bytes``：整本 PDF 合并（可选加密）后的实际字节数，作为总上传体积；
* ``chapters[i].size_bytes``：每章 PDF 的实际字节数，作为分卷权重；
* ``downloaded_bytes``：本次下载图片的原始字节数，用于日志与体积合理性校验；
* ``chapters`` 数量：卷数的天然上限（不拆页，只按章切）；
* 投递方式：文件服务 URL 或 ``base64://``，决定单卷硬上限。

算法步骤：

1. 用 ``whole_bytes / sum(chapter_pdf_bytes)`` 校准每章的上传体积（合并/加密
   带来的比例差异），得到每章权重；
2. 硬上限：URL 投递 200 MiB（协议端大文件随机失败的经验阈值），base64 投递
   256 MiB（协议端 JSON 字符串上限的保守值）；
3. 软目标：URL 100 MiB、base64 160 MiB，尽量让单卷落在稳定区间；
4. 卷数下限 = max(2, ⌈总体积/硬上限⌉)；卷数上限 = min(章节数, ⌈总体积/8MiB⌉,
   24)，其中 8 MiB 是避免碎卷的最小卷体积、24 是避免刷屏的安全上限；
5. 在区间内做顺序保持的线性划分 DP：在「最大卷尽量小」的前提下让各卷均衡，
   取满足软目标的最小卷数；
6. 手动覆盖（``split_auto=false``）：按配置的单卷目标与卷数上限计算卷数，
   仍用同一套 DP 均衡分卷；
7. 实际生成后如果单卷超过硬上限，调用方可传入 ``min_volume_count`` 增卷重排。

所有计算对同一输入是确定性的（同输入同输出），便于测试与排障。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .errors import SplitNotPossibleError
from .models import ChapterPdf, DeliveryMode, MIB, SplitPlan, VolumePlan

# 协议端/QQ 侧经验值：LLBot 8.x 走 HTTP Highway 上传，212 MiB 起偶发 102902，
# 200 MiB 作为硬上限、100 MiB 作为稳定目标。
URL_HARD_LIMIT_BYTES = 200 * MIB
URL_SOFT_TARGET_BYTES = 100 * MIB

# base64 兜底要把整份 PDF 塞进反向 WS 的 JSON 字符串；协议端 V8 单字符串
# 上限约 512 MiB，base64 后对应原始文件约 384 MiB，这里按 256 MiB 保守拒绝。
BASE64_HARD_LIMIT_BYTES = 256 * MIB
BASE64_SOFT_TARGET_BYTES = 160 * MIB

MIN_VOLUME_BYTES = 8 * MIB
MIN_VOLUMES = 2
MAX_VOLUMES = 24
MIN_VOLUME_COUNT = 2

# 合并/加密前后的体积比合理区间，防止缓存里混入异常小/大的章节 PDF
# 把每章权重放大到失真。
MIN_SCALE = 0.5
MAX_SCALE = 4.0

DEFAULT_DP_CHAPTER_LIMIT = 800


def delivery_limits(delivery: DeliveryMode) -> tuple[int, int]:
    """返回 (硬上限, 软目标) 字节数。"""
    if delivery is DeliveryMode.BASE64:
        return BASE64_HARD_LIMIT_BYTES, BASE64_SOFT_TARGET_BYTES
    return URL_HARD_LIMIT_BYTES, URL_SOFT_TARGET_BYTES


@dataclass(frozen=True)
class SplitInput:
    """分卷规划的输入（全部来自本次任务实测）。"""

    chapters: Sequence[ChapterPdf]
    whole_bytes: int
    downloaded_bytes: int = 0
    delivery: DeliveryMode = DeliveryMode.FILE_URL
    manual_target_mb: int = 0
    manual_max_volumes: int = 0
    min_volume_count: int = 0

    def __post_init__(self) -> None:
        if self.whole_bytes < 0:
            raise ValueError("whole_bytes 不能为负数")
        if self.downloaded_bytes < 0:
            raise ValueError("downloaded_bytes 不能为负数")


def plan_split(data: SplitInput) -> SplitPlan:
    """根据实测体积生成分卷方案。"""
    chapters = list(data.chapters)
    if not chapters:
        raise SplitNotPossibleError("没有可发送的章节，无法分卷")
    if len(chapters) < MIN_VOLUMES:
        raise SplitNotPossibleError(
            "整本只有一章，无法按章节边界分卷；请检查网络后重试，"
            "或配置 callback_api_base 改用文件服务 URL"
        )

    raw_sizes = [max(1, int(chapter.size_bytes)) for chapter in chapters]
    raw_total = sum(raw_sizes)
    scale = _estimate_scale(raw_total, data.whole_bytes)
    weights = [max(1, int(round(size * scale))) for size in raw_sizes]
    total = sum(weights)

    hard_limit, soft_limit = delivery_limits(data.delivery)
    if not data.manual_target_mb:
        return _plan_auto(data, chapters, weights, total, hard_limit, soft_limit, scale)
    return _plan_manual(
        data, chapters, weights, total, hard_limit, soft_limit, scale
    )


def _plan_auto(
    data: SplitInput,
    chapters: list[ChapterPdf],
    weights: list[int],
    total: int,
    hard_limit: int,
    soft_limit: int,
    scale: float,
) -> SplitPlan:
    count = len(chapters)
    floor_by_hard = max(MIN_VOLUMES, math.ceil(total / hard_limit))
    floor_by_hard = min(floor_by_hard, count)
    # 实际生成体积可能略高于估算，调用方可要求至少 N 卷（增卷重排）。
    requested = min(max(0, data.min_volume_count), count, MAX_VOLUMES)
    start = max(floor_by_hard, requested, MIN_VOLUME_COUNT)

    ceiling_by_min_volume = max(MIN_VOLUMES, math.ceil(total / MIN_VOLUME_BYTES))
    end = min(count, MAX_VOLUMES, max(start, ceiling_by_min_volume))
    start = min(start, end)

    chosen_partition: list[tuple[int, int]] | None = None
    for k in range(start, end + 1):
        partition, max_size = _partition_minimax(weights, k)
        chosen_partition = partition
        if max_size <= soft_limit:
            break
    assert chosen_partition is not None

    plan = _build_plan(
        chapters=chapters,
        weights=weights,
        partition=chosen_partition,
        hard_limit=hard_limit,
        soft_limit=soft_limit,
        mode="auto",
        scale=scale,
        data=data,
    )
    return plan


def _plan_manual(
    data: SplitInput,
    chapters: list[ChapterPdf],
    weights: list[int],
    total: int,
    hard_limit: int,
    soft_limit: int,
    scale: float,
) -> SplitPlan:
    target_bytes = max(1, min(data.manual_target_mb, 500)) * MIB
    max_volumes = data.manual_max_volumes or MAX_VOLUMES
    max_volumes = max(MIN_VOLUMES, min(max_volumes, 20, len(chapters)))
    count = math.ceil(total / target_bytes)
    count = max(MIN_VOLUMES, min(max_volumes, count, len(chapters)))
    if data.min_volume_count:
        count = min(
            max_volumes,
            len(chapters),
            max(count, data.min_volume_count),
        )

    partition, _ = _partition_minimax(weights, count)
    return _build_plan(
        chapters=chapters,
        weights=weights,
        partition=partition,
        hard_limit=hard_limit,
        soft_limit=target_bytes,
        mode="manual",
        scale=scale,
        data=data,
    )


def _build_plan(
    *,
    chapters: list[ChapterPdf],
    weights: list[int],
    partition: Sequence[tuple[int, int]],
    hard_limit: int,
    soft_limit: int,
    mode: str,
    scale: float,
    data: SplitInput,
) -> SplitPlan:
    volumes: list[VolumePlan] = []
    for number, (start, end) in enumerate(partition, start=1):
        volume_chapters = tuple(chapters[start:end])
        if not volume_chapters:
            continue
        estimated = sum(weights[start:end])
        volumes.append(
            VolumePlan(
                number=number,
                chapters=volume_chapters,
                estimated_bytes=estimated,
            )
        )
    if not volumes:
        raise SplitNotPossibleError("分卷规划没有得到有效方案")
    target_bytes = max(volume.estimated_bytes for volume in volumes)
    return SplitPlan(
        volumes=tuple(volumes),
        target_bytes=target_bytes,
        soft_limit_bytes=soft_limit,
        hard_limit_bytes=hard_limit,
        mode=mode,
        exceeds_hard_limit=target_bytes > hard_limit,
        basis=_describe_basis(data, scale, target_bytes, len(volumes)),
    )


def _estimate_scale(raw_total: int, whole_bytes: int) -> float:
    """按整本实测大小校准每章 PDF 的上传体积比例。"""
    if whole_bytes > 0 and raw_total > 0:
        scale = whole_bytes / raw_total
        return max(MIN_SCALE, min(MAX_SCALE, scale))
    return 1.0


def _describe_basis(
    data: SplitInput, scale: float, target_bytes: int, volume_count: int
) -> str:
    image_mib = data.downloaded_bytes / MIB
    whole_mib = data.whole_bytes / MIB
    raw_mib = sum(chapter.size_bytes for chapter in data.chapters) / MIB
    return (
        f"整本 {whole_mib:.1f} MiB、章节 PDF 合计 {raw_mib:.1f} MiB、"
        f"图片下载 {image_mib:.1f} MiB、{len(data.chapters)} 章 → "
        f"{volume_count} 卷，预计最大卷 {target_bytes / MIB:.1f} MiB"
        f"（体积系数 {scale:.2f}，{data.delivery.value}）"
    )


def _partition_minimax(
    weights: Sequence[int], group_count: int
) -> tuple[list[tuple[int, int]], int]:
    """顺序保持的线性划分 DP。

    返回 (分组区间列表, 最大组权重和)。目标函数按「最大组尽量小」优先，
    相同时选平方和更小（更均衡）的方案；同输入结果确定。
    ``group_count`` 必须落在 ``[1, len(weights)]``。
    """
    count = len(weights)
    if group_count < 1 or group_count > count:
        raise ValueError("group_count 超出章节数范围")
    if count > DEFAULT_DP_CHAPTER_LIMIT:
        return _greedy_partition(weights, group_count)

    prefix = [0] * (count + 1)
    for index, weight in enumerate(weights, start=1):
        prefix[index] = prefix[index - 1] + weight

    # best[g][i] = (最大组权重, 平方和)，表示前 i 项分成 g 组的最优值
    infinity = (float("inf"), float("inf"))
    best: list[list[tuple[float, float]]] = [
        [infinity] * (count + 1) for _ in range(group_count + 1)
    ]
    cut: list[list[int]] = [[0] * (count + 1) for _ in range(group_count + 1)]
    best[0][0] = (0, 0)

    for groups in range(1, group_count + 1):
        for end in range(groups, count + 1):
            for start in range(groups - 1, end):
                previous = best[groups - 1][start]
                if previous[0] == float("inf"):
                    continue
                segment = prefix[end] - prefix[start]
                candidate = (max(previous[0], segment), previous[1] + segment * segment)
                if candidate < best[groups][end]:
                    best[groups][end] = candidate
                    cut[groups][end] = start

    if best[group_count][count][0] == float("inf"):
        raise SplitNotPossibleError("分卷规划失败：无法在章节边界上切分")

    boundaries: list[tuple[int, int]] = []
    end = count
    for groups in range(group_count, 0, -1):
        start = cut[groups][end]
        boundaries.append((start, end))
        end = start
    boundaries.reverse()
    return boundaries, int(best[group_count][count][0])


def _greedy_partition(
    weights: Sequence[int], group_count: int
) -> tuple[list[tuple[int, int]], int]:
    """超长章节列表的兜底分组：按累积权重尽量等分，仍只落在章节边界。"""
    import bisect

    count = len(weights)
    prefix = [0] * (count + 1)
    for index, weight in enumerate(weights, start=1):
        prefix[index] = prefix[index - 1] + weight

    boundaries: list[tuple[int, int]] = []
    start = 0
    for groups_left in range(group_count, 1, -1):
        target = (prefix[count] - prefix[start]) / groups_left
        # 后面还剩 groups_left-1 组，每组至少一章，因此 end 上限是 count-groups_left+1
        high = count - groups_left + 2
        end = bisect.bisect_left(
            prefix, prefix[start] + target, start + 1, high
        )
        end = max(start + 1, min(end, count - groups_left + 1))
        boundaries.append((start, end))
        start = end
    boundaries.append((start, count))
    max_size = max(sum(weights[a:b]) for a, b in boundaries)
    return boundaries, max_size
