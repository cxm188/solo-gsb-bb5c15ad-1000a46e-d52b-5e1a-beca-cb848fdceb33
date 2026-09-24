"""API 端到端测试（需要 fastapi/httpx，容器内或安装依赖后运行）。"""
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


def test_full_flow_and_correction_recompute(client):
    port_id = _setup_port(client)

    # 吃水 5.0 + 富余 0.5 = 要求水深 5.5；基准 4.5 → 需潮高 >= 1.0
    # 水深 5.0→7.5→5.0，达标窗口 [00:36, 05:24]，通航 60 分钟
    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["required_depth"] == 5.5
    assert _norm(body["earliest_departure"]) == "2026-09-24T00:36:00+00:00"
    assert [{k: _norm(v) for k, v in w.items()} for w in body["windows"]] == [
        {"start": "2026-09-24T00:36:00+00:00", "end": "2026-09-24T04:24:00+00:00"}
    ]

    # 更正高潮预报点 3.0 → 1.0：水深峰值仅 5.5，只剩零长窗口，不足 60 分钟
    r = client.put(
        f"/ports/{port_id}/tide-forecasts",
        json={"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]},
    )
    assert r.status_code == 200

    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    body = r.json()
    assert body["windows"] == []
    assert body["earliest_departure"] is None


def test_validation_rejects_non_positive_duration(client):
    port_id = _setup_port(client)
    for bad in (0, -30):
        r = client.post(
            f"/ports/{port_id}/windows",
            json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": bad},
        )
        assert r.status_code == 422


def test_unknown_port_404(client):
    assert client.post(
        "/ports/999/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    ).status_code == 404


def test_no_forecasts_returns_empty(client):
    r = client.post("/ports", json={"name": "空港", "reference_depth": 10.0})
    port_id = r.json()["id"]
    r = client.post(
        f"/ports/{port_id}/windows",
        json={"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60},
    )
    assert r.status_code == 200
    assert r.json()["windows"] == []
    assert r.json()["earliest_departure"] is None
