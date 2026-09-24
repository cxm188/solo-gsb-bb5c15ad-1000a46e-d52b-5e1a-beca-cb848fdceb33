"""核心窗口计算算法的单元测试（仅标准库）。"""
from datetime import datetime, timedelta, timezone

from app.tides import departure_windows, sufficient_depth_windows

BASE = datetime(2026, 9, 24, tzinfo=timezone.utc)


def pts(*heights: float, step_minutes: int = 60):
    """从 BASE 起按等间隔生成 (时刻, 潮高) 序列。"""
    return [
        (BASE + timedelta(minutes=step_minutes * i), h) for i, h in enumerate(heights)
    ]


def test_single_tide_cycle_window():
    # 基准水深 3，潮高 1→3→1：水深 4→6→4，要求 5
    # 上升段 00:30 达标，下降段 01:30 退出
    windows = sufficient_depth_windows(pts(1, 3, 1), reference_depth=3.0, required_depth=5.0)
    assert len(windows) == 1
    start, end = windows[0]
    assert start == (BASE + timedelta(minutes=30)).timestamp()
    assert end == (BASE + timedelta(hours=1, minutes=30)).timestamp()


def test_exact_threshold_endpoints_included():
    # 水深恰好全程等于要求值，端点恰好达标也纳入
    windows = sufficient_depth_windows(pts(2, 2), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=1)).timestamp())]


def test_departure_window_subtracts_duration():
    # 达标窗口 [00:30, 01:30]，通航 30 分钟 → 可出发 [00:30, 01:00]
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=1800
    )
    assert len(deps) == 1
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1)
    assert earliest == BASE + timedelta(minutes=30)


def test_window_shorter_than_duration_discarded():
    # 达标窗口 60 分钟 < 所需 90 分钟 → 舍弃
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=5400
    )
    assert deps == []
    assert earliest is None


def test_duration_exactly_equals_window_kept():
    # 窗口 60 分钟 == 所需 60 分钟 → 保留，可出发时段退化为同一时刻
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=3600
    )
    assert len(deps) == 1
    assert deps[0][0] == deps[0][1] == BASE + timedelta(minutes=30)
    assert earliest == BASE + timedelta(minutes=30)


def test_multiple_windows():
    # 潮高 1,3,1,3,1（基准 3，要求 5）：两个独立高潮窗口 [00:30,01:30]、[02:30,03:30]
    deps, earliest = departure_windows(
        pts(1, 3, 1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert len(deps) == 2
    # 第一个窗口 [00:30, 01:30]，可出发 [00:30, 01:29]
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1, minutes=29)
    # 第二个窗口 [02:30, 03:30]
    assert deps[1][0] == BASE + timedelta(hours=2, minutes=30)
    assert deps[1][1] == BASE + timedelta(hours=3, minutes=29)
    assert earliest == deps[0][0]


def test_all_below_required():
    deps, earliest = departure_windows(
        pts(0.5, 1.0, 0.5), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_fewer_than_two_points_no_coverage():
    deps, earliest = departure_windows(
        pts(5.0), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_window_clipped_at_forecast_coverage():
    # 全程达标时窗口就是预报覆盖时段本身，不外推
    windows = sufficient_depth_windows(pts(3, 4, 3), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=2)).timestamp())]
