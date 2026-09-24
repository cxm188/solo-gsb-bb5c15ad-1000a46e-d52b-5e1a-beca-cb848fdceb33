"""核心窗口计算算法的单元测试（仅标准库）。"""
from datetime import datetime, timedelta, timezone

from app.tides import departure_windows, earliest_departure_slot, sufficient_depth_windows

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


# ---- earliest_departure_slot：预约排程 ----


def test_slot_without_occupation():
    # 无占用时取可出发时段与请求范围的交集起点
    windows = [(BASE + timedelta(minutes=30), BASE + timedelta(hours=2))]
    t = earliest_departure_slot(windows, BASE, BASE + timedelta(hours=3), 1800, [])
    assert t == BASE + timedelta(minutes=30)


def test_slot_respects_not_before():
    # 请求范围比可出发时段更晚，按 not_before 出发
    windows = [(BASE, BASE + timedelta(hours=2))]
    t = earliest_departure_slot(windows, BASE + timedelta(hours=1), BASE + timedelta(hours=3), 1800, [])
    assert t == BASE + timedelta(hours=1)


def test_slot_pushed_past_occupied_back_to_back():
    # 与已有预约重叠时顺延到其结束时刻（首尾相接允许）
    windows = [(BASE, BASE + timedelta(hours=4))]
    occupied = [(BASE, BASE + timedelta(hours=1))]
    t = earliest_departure_slot(windows, BASE, BASE + timedelta(hours=4), 3600, occupied)
    assert t == BASE + timedelta(hours=1)


def test_slot_uses_gap_between_occupied():
    # 两段占用之间恰好有 1 小时空档（两端均首尾相接）
    windows = [(BASE, BASE + timedelta(hours=6))]
    occupied = [
        (BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
        (BASE, BASE + timedelta(hours=1)),
    ]
    t = earliest_departure_slot(windows, BASE, BASE + timedelta(hours=6), 3600, occupied)
    assert t == BASE + timedelta(hours=1)


def test_slot_none_when_fully_occupied():
    windows = [(BASE, BASE + timedelta(hours=2))]
    occupied = [(BASE, BASE + timedelta(hours=3))]
    assert earliest_departure_slot(windows, BASE, BASE + timedelta(hours=2), 3600, occupied) is None


def test_slot_none_when_request_range_outside_windows():
    windows = [(BASE, BASE + timedelta(hours=2))]
    assert earliest_departure_slot(
        windows, BASE + timedelta(hours=3), BASE + timedelta(hours=4), 3600, []
    ) is None


def test_slot_falls_through_to_next_window():
    # 第一个可出发时段被占满时使用下一个
    windows = [
        (BASE, BASE + timedelta(hours=1)),
        (BASE + timedelta(hours=2), BASE + timedelta(hours=4)),
    ]
    occupied = [(BASE, BASE + timedelta(hours=2))]
    t = earliest_departure_slot(windows, BASE, BASE + timedelta(hours=6), 3600, occupied)
    assert t == BASE + timedelta(hours=2)
