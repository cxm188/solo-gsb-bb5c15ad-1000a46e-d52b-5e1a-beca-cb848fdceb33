"""港口潮汐通航窗口与单向航道预约 API。

- 录入港口（航道基准水深）与潮高预报点；
- 相邻预报点线性插值，在预报覆盖时段内求水深 >= 吃水 + 安全富余的连续窗口；
- 窗口结束时刻减去通航时长得到可出发时段，不足时长的窗口舍弃；
- 查询时实时读取最新预报数据计算，预报点更正后立即生效；
- 单向航道预约：在允许出发范围内按最新预报安排最早可出发时刻，整个通航
  过程不与该港口有效预约重叠（首尾相接允许）；检查与落库在同一事务内完成，
  并发创建不会双重占用；预约创建后即固定，预报更正不追溯取消已有预约。
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlite3 import Connection

from .database import get_db, init_db
from .schemas import (
    DepartureWindow,
    PortCreate,
    PortOut,
    PortUpdate,
    ReservationCreate,
    ReservationOut,
    TideForecastOut,
    TideForecastUpsert,
    WindowQuery,
    WindowQueryResult,
)
from .tides import departure_windows, earliest_departure_slot

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # 启动时初始化数据库（幂等）
    yield


app = FastAPI(
    title="港口潮汐通航窗口与预约 API",
    version="1.1.0",
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


def _load_forecast_points(db: Connection, port_id: int):
    """读取该港口当前全部预报点，按时刻升序。"""
    rows = db.execute(
        "SELECT forecast_time, tide_height FROM tide_forecasts "
        "WHERE port_id = ? ORDER BY forecast_time",
        (port_id,),
    ).fetchall()
    return [(datetime.fromisoformat(r["forecast_time"]), r["tide_height"]) for r in rows]


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
    points = _load_forecast_points(db, port_id)

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


@app.post("/ports/{port_id}/reservations", response_model=ReservationOut, status_code=201)
def create_reservation(port_id: int, payload: ReservationCreate, db: Connection = Depends(get_db)):
    """创建单向航道预约。

    基于该港口最新预报计算可出发时段，在 [not_before, not_after] 内安排最早
    出发时刻，且整个通航过程（出发时刻 + 通航时长）不与该港口其他有效预约
    重叠（首尾相接允许）。没有可用时刻返回 409，不写入任何数据。

    “读取占用 → 计算空档 → 写入”在同一事务内完成（BEGIN IMMEDIATE 立即获取
    写锁，配合 busy_timeout 排队），同港口并发创建串行执行，不会双重占用。
    预约一旦创建即固定，后续预报更正不追溯取消。
    """
    port = _get_port_or_404(db, port_id)
    not_before = _normalize(payload.not_before)
    not_after = _normalize(payload.not_after)
    if not_after < not_before:
        raise HTTPException(status_code=422, detail="not_after 必须不早于 not_before")

    db.execute("BEGIN IMMEDIATE")

    points = _load_forecast_points(db, port_id)
    duration_seconds = payload.duration_minutes * 60
    windows, _ = departure_windows(
        points,
        reference_depth=port["reference_depth"],
        required_depth=payload.draft + payload.safety_margin,
        duration_seconds=duration_seconds,
    )

    occ_rows = db.execute(
        "SELECT departure_time, arrival_time FROM reservations "
        "WHERE port_id = ? AND status = 'active'",
        (port_id,),
    ).fetchall()
    occupied = [
        (datetime.fromisoformat(r["departure_time"]), datetime.fromisoformat(r["arrival_time"]))
        for r in occ_rows
    ]

    departure = earliest_departure_slot(windows, not_before, not_after, duration_seconds, occupied)
    if departure is None:
        # 事务随连接关闭回滚，不产生任何写入
        raise HTTPException(status_code=409, detail="请求范围内没有可用的出发时刻")

    arrival = departure + timedelta(seconds=duration_seconds)
    now = datetime.now(timezone.utc)
    cur = db.execute(
        """
        INSERT INTO reservations (
            port_id, vessel_name, draft, safety_margin, duration_minutes,
            not_before, not_after, departure_time, arrival_time, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            port_id, payload.vessel_name, payload.draft, payload.safety_margin,
            payload.duration_minutes, not_before.isoformat(), not_after.isoformat(),
            departure.isoformat(), arrival.isoformat(), now.isoformat(),
        ),
    )
    return ReservationOut(
        id=cur.lastrowid,
        port_id=port_id,
        vessel_name=payload.vessel_name,
        draft=payload.draft,
        safety_margin=payload.safety_margin,
        duration_minutes=payload.duration_minutes,
        not_before=not_before,
        not_after=not_after,
        departure_time=departure,
        arrival_time=arrival,
        status="active",
        created_at=now,
    )


@app.get("/ports/{port_id}/reservations", response_model=list[ReservationOut])
def list_reservations(port_id: int, db: Connection = Depends(get_db)):
    """按出发时刻升序列出该港口的有效预约（不含已取消）。"""
    _get_port_or_404(db, port_id)
    rows = db.execute(
        "SELECT * FROM reservations WHERE port_id = ? AND status = 'active' "
        "ORDER BY departure_time, id",
        (port_id,),
    ).fetchall()
    return [ReservationOut(**dict(r)) for r in rows]


@app.delete("/ports/{port_id}/reservations/{reservation_id}", status_code=204)
def cancel_reservation(port_id: int, reservation_id: int, db: Connection = Depends(get_db)):
    """取消预约（软删除）：释放其占用的通航时段；不存在或已取消返回 404。"""
    _get_port_or_404(db, port_id)
    cur = db.execute(
        "UPDATE reservations SET status = 'cancelled' "
        "WHERE id = ? AND port_id = ? AND status = 'active'",
        (reservation_id, port_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"预约 {reservation_id} 不存在或已取消")
