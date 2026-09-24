"""潮汐通航窗口核心计算（纯标准库，便于单测）。

规则：
- 实际水深(t) = 航道基准水深 + 潮高(t)；
- 相邻预报点之间潮高按线性插值；
- 只在预报覆盖时段 [首个预报点, 末个预报点] 内计算；
- 水深 >= 吃水 + 安全富余 的连续时段为达标窗口，端点恰好达标也纳入；
- 可出发时段 = [窗口起点, 窗口终点 − 通航时长]；窗口长度不足通航时长的舍弃。
"""
from __future__ import annotations

from datetime import datetime, timezone

_EPS = 1e-9  # 浮点容差，保证“恰好达标”的端点被纳入

ForecastPoint = tuple[datetime, float]  # (预报时刻, 潮高)


def _cross_time(t0: float, d0: float, t1: float, d1: float, required: float) -> float:
    """线性插值下水深恰好等于 required 的时刻（Unix 秒）。"""
    frac = (required - d0) / (d1 - d0)
    return t0 + frac * (t1 - t0)


def sufficient_depth_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
) -> list[tuple[float, float]]:
    """返回水深达标的连续窗口列表 [(start_ts, end_ts)]（Unix 秒，按时间升序）。

    points 需按时刻升序且时刻唯一；不足 2 个预报点时无插值区间，返回空。
    """
    if len(points) < 2:
        return []

    ts = [p[0].timestamp() for p in points]
    depths = [reference_depth + p[1] for p in points]

    windows: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    def flush() -> None:
        nonlocal cur_start, cur_end
        if cur_start is not None:
            windows.append((cur_start, cur_end))  # type: ignore[arg-type]
            cur_start = cur_end = None

    for i in range(len(points) - 1):
        t0, t1 = ts[i], ts[i + 1]
        d0, d1 = depths[i], depths[i + 1]
        above0 = d0 >= required_depth - _EPS
        above1 = d1 >= required_depth - _EPS

        if above0 and above1:
            seg = (t0, t1)
        elif not above0 and not above1:
            # 线性段两端都不达标，整段不达标；关闭当前窗口
            flush()
            continue
        elif above0:
            seg = (t0, _cross_time(t0, d0, t1, d1, required_depth))
        else:
            seg = (_cross_time(t0, d0, t1, d1, required_depth), t1)

        if cur_start is None:
            cur_start, cur_end = seg
        elif seg[0] <= cur_end + 1e-6:  # type: ignore[operator]
            # 相邻段首尾相接，合并为同一连续窗口
            cur_end = max(cur_end, seg[1])  # type: ignore[type-var]
        else:
            flush()
            cur_start, cur_end = seg

    flush()
    return windows


def departure_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
    duration_seconds: float,
) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """计算全部可出发时段与最早出发时刻。

    返回 ([(start, end), ...], earliest)，时间均为 UTC datetime。
    """
    result: list[tuple[datetime, datetime]] = []
    for start_ts, end_ts in sufficient_depth_windows(points, reference_depth, required_depth):
        if end_ts - start_ts >= duration_seconds - _EPS:
            result.append((_to_utc(start_ts), _to_utc(end_ts - duration_seconds)))
    earliest = min((w[0] for w in result), default=None)
    return result, earliest


def _to_utc(ts: float) -> datetime:
    """Unix 秒转 UTC datetime，四舍五入到整秒避免浮点噪声。"""
    return datetime.fromtimestamp(round(ts), tz=timezone.utc)
