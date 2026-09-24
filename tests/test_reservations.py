"""单向航道预约的 API 端到端测试（需要 fastapi/httpx）。"""
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


def _norm(s: str) -> str:
    """规范化 ISO 8601 时刻字符串，使 Z 与 +00:00 等等价表示可比较。"""
    return datetime.fromisoformat(s).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个测试用例使用独立的临时数据库文件
    monkeypatch.setattr(database, "DATABASE_PATH", str(tmp_path / "test.db"))
    database.init_db()
    with TestClient(app) as c:
        yield c


def _setup_port(client):
    """基准水深 4.5；潮高 0.5→3.0→0.5 → 水深 5.0→7.5→5.0。

    吃水 5.0 + 富余 0.5 = 要求水深 5.5 → 达标窗口 [00:36, 05:24]，
    通航 60 分钟时可出发时段为 [00:36, 04:24]。
    """
    r = client.post("/ports", json={"name": "示例港", "reference_depth": 4.5})
    assert r.status_code == 201
    port_id = r.json()["id"]
    r = client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={
            "points": [
                {"forecast_time": "2026-09-24T00:00:00Z", "tide_height": 0.5},
                {"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 3.0},
                {"forecast_time": "2026-09-24T06:00:00Z", "tide_height": 0.5},
            ]
        },
    )
    assert r.status_code == 200
    return port_id


def _reserve(client, port_id, vessel="远洋号", duration=60,
             not_before="2026-09-24T00:00:00Z", not_after="2026-09-24T06:00:00Z"):
    return client.post(
        f"/ports/{port_id}/reservations",
        json={
            "vessel_name": vessel,
            "draft": 5.0,
            "safety_margin": 0.5,
            "duration_minutes": duration,
            "not_before": not_before,
            "not_after": not_after,
        },
    )


def test_create_reservation_picks_earliest_departure(client):
    port_id = _setup_port(client)
    r = _reserve(client, port_id)
    assert r.status_code == 201
    body = r.json()
    assert body["vessel_name"] == "远洋号"
    assert body["status"] == "active"
    # 可出发时段 [00:36, 04:24] 与请求范围 [00:00, 06:00] 的交集起点
    assert _norm(body["departure_time"]) == "2026-09-24T00:36:00+00:00"
    assert _norm(body["arrival_time"]) == "2026-09-24T01:36:00+00:00"  # +60 分钟
    assert _norm(body["not_before"]) == "2026-09-24T00:00:00+00:00"


def test_not_before_clips_departure(client):
    port_id = _setup_port(client)
    r = _reserve(client, port_id, not_before="2026-09-24T02:00:00Z")
    assert r.status_code == 201
    assert _norm(r.json()["departure_time"]) == "2026-09-24T02:00:00+00:00"


def test_naive_and_zoned_times_interpreted_as_utc(client):
    port_id = _setup_port(client)
    # 10:00+08:00 == 02:00 UTC；无时区按 UTC 处理
    r = _reserve(client, port_id, not_before="2026-09-24T10:00:00+08:00")
    assert r.status_code == 201
    assert _norm(r.json()["departure_time"]) == "2026-09-24T02:00:00+00:00"
    assert _norm(r.json()["not_before"]) == "2026-09-24T02:00:00+00:00"


def test_second_reservation_back_to_back(client):
    port_id = _setup_port(client)
    r1 = _reserve(client, port_id, vessel="甲")
    assert r1.status_code == 201
    r2 = _reserve(client, port_id, vessel="乙")
    assert r2.status_code == 201
    # 第一艘占 [00:36, 01:36]，第二艘首尾相接从 01:36 出发
    assert _norm(r2.json()["departure_time"]) == "2026-09-24T01:36:00+00:00"
    assert _norm(r2.json()["arrival_time"]) == "2026-09-24T02:36:00+00:00"


def test_fully_occupied_returns_409_and_writes_nothing(client):
    port_id = _setup_port(client)
    # 达标窗口恰好 288 分钟，一艘船占满 [00:36, 05:24]
    r1 = _reserve(client, port_id, vessel="大块头", duration=288)
    assert r1.status_code == 201
    assert _norm(r1.json()["departure_time"]) == "2026-09-24T00:36:00+00:00"

    r2 = _reserve(client, port_id, vessel="排队者")
    assert r2.status_code == 409

    # 409 不写入：列表仍只有第一条
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert [r["vessel_name"] for r in listed] == ["大块头"]


def test_no_forecast_coverage_returns_409(client):
    r = client.post("/ports", json={"name": "空港", "reference_depth": 10.0})
    port_id = r.json()["id"]
    assert _reserve(client, port_id).status_code == 409


def test_list_reservations_sorted_by_departure(client):
    port_id = _setup_port(client)
    _reserve(client, port_id, vessel="A", not_before="2026-09-24T03:00:00Z")
    _reserve(client, port_id, vessel="B", not_before="2026-09-24T01:00:00Z")
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert [r["vessel_name"] for r in listed] == ["B", "A"]
    assert _norm(listed[0]["departure_time"]) <= _norm(listed[1]["departure_time"])


def test_cancel_reservation_frees_slot(client):
    port_id = _setup_port(client)
    r1 = _reserve(client, port_id, vessel="大块头", duration=288)
    rid = r1.json()["id"]
    assert _reserve(client, port_id, vessel="排队者").status_code == 409

    # 取消后时段释放，同一请求立即可约
    assert client.delete(f"/ports/{port_id}/reservations/{rid}").status_code == 204
    assert client.get(f"/ports/{port_id}/reservations").json() == []
    r2 = _reserve(client, port_id, vessel="排队者")
    assert r2.status_code == 201
    assert _norm(r2.json()["departure_time"]) == "2026-09-24T00:36:00+00:00"


def test_cancel_missing_or_already_cancelled_404(client):
    port_id = _setup_port(client)
    assert client.delete(f"/ports/{port_id}/reservations/42").status_code == 404
    rid = _reserve(client, port_id).json()["id"]
    assert client.delete(f"/ports/{port_id}/reservations/{rid}").status_code == 204
    # 重复取消返回 404
    assert client.delete(f"/ports/{port_id}/reservations/{rid}").status_code == 404


def test_forecast_correction_does_not_cancel_reservation(client):
    port_id = _setup_port(client)
    assert _reserve(client, port_id).status_code == 201
    # 更正高潮预报点 3.0 → 1.0，窗口消失，但已有预约不追溯取消
    r = client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]},
    )
    assert r.status_code == 200
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert len(listed) == 1
    assert listed[0]["status"] == "active"
    assert _norm(listed[0]["departure_time"]) == "2026-09-24T00:36:00+00:00"


def test_reservation_unknown_port_404(client):
    assert _reserve(client, 999).status_code == 404
    assert client.get("/ports/999/reservations").status_code == 404
    assert client.delete("/ports/999/reservations/1").status_code == 404


def test_invalid_time_range_422(client):
    port_id = _setup_port(client)
    r = _reserve(
        client, port_id,
        not_before="2026-09-24T04:00:00Z", not_after="2026-09-24T01:00:00Z",
    )
    assert r.status_code == 422


def test_invalid_numeric_fields_422(client):
    port_id = _setup_port(client)
    assert _reserve(client, port_id, duration=0).status_code == 422
    assert _reserve(client, port_id, duration=-30).status_code == 422


def test_concurrent_creation_no_double_booking(client):
    """同港口并发创建：成功的预约通航区间两两不重叠，失败的不落库。"""
    port_id = _setup_port(client)
    n_threads = 6
    results = []

    def worker(i):
        c = TestClient(app)  # 每线程独立客户端（不重复触发 lifespan）
        results.append(_reserve(c, port_id, vessel=f"船{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [r.json() for r in results if r.status_code == 201]
    assert len(created) >= 1
    # 可出发时段 [00:36, 04:24] 按 60 分钟首尾相接最多容纳 4 艘
    assert len(created) == 4
    # 最早的一艘从 00:36 出发
    intervals = sorted((r["departure_time"], r["arrival_time"]) for r in created)
    assert _norm(intervals[0][0]) == "2026-09-24T00:36:00+00:00"
    # 所有成功预约的通航区间互不重叠（首尾相接允许）
    for (_, end), (start, _) in zip(intervals, intervals[1:]):
        assert _norm(end) <= _norm(start)
    # 落库数量与成功数一致，409 的请求没有写入
    listed = client.get(f"/ports/{port_id}/reservations").json()
    assert len(listed) == len(created)
