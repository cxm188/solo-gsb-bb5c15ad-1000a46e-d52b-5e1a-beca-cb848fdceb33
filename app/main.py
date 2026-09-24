"""港口潮汐通航窗口 API。

- 录入港口（航道基准水深）与潮高预报点；
- 相邻预报点线性插值，在预报覆盖时段内求水深 >= 吃水 + 安全富余的连续窗口；
- 窗口结束时刻减去通航时长得到可出发时段，不足时长的窗口舍弃；
- 查询时实时读取最新预报数据计算，预报点更正后立即生效。
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlite3 import Connection

from .database import get_db, init_db
from .schemas import (
    DepartureWindow,
    PortCreate,
    PortOut,
    PortUpdate,
    TideForecastOut,
    TideForecastUpsert,
    WindowQuery,
    WindowQueryResult,
)
from .tides import departure_windows

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # 启动时初始化数据库（幂等）
    yield


app = FastAPI(
    title="港口潮汐通航窗口 API",
    version="1.0.0",
    lifespan=lifespan,
)


def _normalize(dt: datetime) -> datetime:
    """无时区信息按 UTC 处理，其余统一换算为 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_port_or_404(db: Connection, port_id: int):
    row = db.execute(
        "SELECT id, name, reference_depth FROM ports WHERE id = ?", (port_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"港口 {port_id} 不存在")
    return row


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ports", response_model=PortOut, status_code=201)
def create_port(payload: PortCreate, db: Connection = Depends(get_db)):
    """录入港口：名称 + 航道基准水深。"""
    cur = db.execute(
        "INSERT INTO ports (name, reference_depth) VALUES (?, ?)",
        (payload.name, payload.reference_depth),
    )
    return PortOut(id=cur.lastrowid, name=payload.name, reference_depth=payload.reference_depth)


@app.get("/ports", response_model=list[PortOut])
def list_ports(db: Connection = Depends(get_db)):
    rows = db.execute("SELECT id, name, reference_depth FROM ports ORDER BY id").fetchall()
    return [PortOut(**dict(r)) for r in rows]


@app.get("/ports/{port_id}", response_model=PortOut)
def get_port(port_id: int, db: Connection = Depends(get_db)):
    return PortOut(**dict(_get_port_or_404(db, port_id)))


@app.patch("/ports/{port_id}", response_model=PortOut)
def update_port(port_id: int, payload: PortUpdate, db: Connection = Depends(get_db)):
    """更正港口名称或航道基准水深。"""
    row = _get_port_or_404(db, port_id)
    name = payload.name if payload.name is not None else row["name"]
    depth = payload.reference_depth if payload.reference_depth is not None else row["reference_depth"]
    db.execute(
        "UPDATE ports SET name = ?, reference_depth = ? WHERE id = ?",
        (name, depth, port_id),
    )
    return PortOut(id=port_id, name=name, reference_depth=depth)


@app.put("/ports/{port_id}/tide-forecasts", response_model=list[TideForecastOut])
def upsert_tide_forecasts(
    port_id: int, payload: TideForecastUpsert, db: Connection = Depends(get_db)
):
    """批量录入潮高预报点；同一时刻重复提交即更正（覆盖旧值）。"""
    _get_port_or_404(db, port_id)
    for p in payload.points:
        t = _normalize(p.forecast_time).isoformat()
        db.execute(
            """
            INSERT INTO tide_forecasts (port_id, forecast_time, tide_height)
            VALUES (?, ?, ?)
            ON CONFLICT (port_id, forecast_time)
            DO UPDATE SET tide_height = excluded.tide_height
            """,
            (port_id, t, p.tide_height),
        )
    return _list_forecasts(db, port_id)


@app.get("/ports/{port_id}/tide-forecasts", response_model=list[TideForecastOut])
def get_tide_forecasts(port_id: int, db: Connection = Depends(get_db)):
    _get_port_or_404(db, port_id)
    return _list_forecasts(db, port_id)


def _list_forecasts(db: Connection, port_id: int) -> list[TideForecastOut]:
    rows = db.execute(
        "SELECT forecast_time, tide_height FROM tide_forecasts "
        "WHERE port_id = ? ORDER BY forecast_time",
        (port_id,),
    ).fetchall()
    return [
        TideForecastOut(forecast_time=datetime.fromisoformat(r["forecast_time"]), tide_height=r["tide_height"])
        for r in rows
    ]


@app.post("/ports/{port_id}/windows", response_model=WindowQueryResult)
def query_windows(port_id: int, payload: WindowQuery, db: Connection = Depends(get_db)):
    """船舶提交吃水、安全富余、通航时长，返回全部可出发时段与最早出发时刻。

    每次查询都基于数据库中最新的预报点实时重算，预报点更正后立即生效。
    """
    port = _get_port_or_404(db, port_id)
    rows = db.execute(
        "SELECT forecast_time, tide_height FROM tide_forecasts "
        "WHERE port_id = ? ORDER BY forecast_time",
        (port_id,),
    ).fetchall()
    points = sorted(
        ((datetime.fromisoformat(r["forecast_time"]), r["tide_height"]) for r in rows),
        key=lambda p: p[0],
    )

    required_depth = payload.draft + payload.safety_margin
    windows, earliest = departure_windows(
        points,
        reference_depth=port["reference_depth"],
        required_depth=required_depth,
        duration_seconds=payload.duration_minutes * 60,
    )
    return WindowQueryResult(
        port_id=port_id,
        required_depth=required_depth,
        duration_minutes=payload.duration_minutes,
        earliest_departure=earliest,
        windows=[DepartureWindow(start=s, end=e) for s, e in windows],
    )
