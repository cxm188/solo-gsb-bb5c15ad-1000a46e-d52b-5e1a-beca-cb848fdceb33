"""SQLite 连接与建表初始化。

数据库路径通过环境变量 DATABASE_PATH 配置（默认 tide.db）。
建表在服务启动时执行（CREATE TABLE IF NOT EXISTS，幂等），
因此 `docker compose up --build` 一条命令即可完成依赖安装、
数据库初始化和服务启动。
"""
import os
import sqlite3
from pathlib import Path

DATABASE_PATH = os.getenv("DATABASE_PATH", "tide.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    reference_depth REAL    NOT NULL          -- 航道基准水深（米）
);

CREATE TABLE IF NOT EXISTS tide_forecasts (
    port_id       INTEGER NOT NULL
        REFERENCES ports(id) ON DELETE CASCADE,
    forecast_time TEXT    NOT NULL,           -- ISO 8601（统一存为 UTC）
    tide_height   REAL    NOT NULL,           -- 潮高（米，相对海图基准面）
    PRIMARY KEY (port_id, forecast_time)      -- 同一时刻重复录入即“更正”
);

CREATE TABLE IF NOT EXISTS reservations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    port_id          INTEGER NOT NULL
        REFERENCES ports(id) ON DELETE CASCADE,
    vessel_name      TEXT    NOT NULL,        -- 船名
    draft            REAL    NOT NULL,        -- 吃水（米）
    safety_margin    REAL    NOT NULL,        -- 安全富余（米）
    duration_minutes REAL    NOT NULL,        -- 通航时长（分钟）
    not_before       TEXT    NOT NULL,        -- 允许出发的最早时刻（UTC ISO 8601）
    not_after        TEXT    NOT NULL,        -- 允许出发的最晚时刻（UTC ISO 8601）
    departure_time   TEXT    NOT NULL,        -- 安排的出发时刻（UTC ISO 8601）
    arrival_time     TEXT    NOT NULL,        -- 通航结束时刻 = 出发 + 通航时长
    status           TEXT    NOT NULL DEFAULT 'active',  -- active=有效 / cancelled=已取消
    created_at       TEXT    NOT NULL         -- 创建时间（UTC ISO 8601）
);
"""


def init_db() -> None:
    """创建数据目录与表结构（幂等）。"""
    db_path = Path(DATABASE_PATH)
    if db_path.parent and str(db_path.parent) != ".":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executescript(SCHEMA)


def get_db():
    """FastAPI 依赖：每个请求一个 SQLite 连接，正常返回时提交。"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写锁占用时最多等待 10 秒再报 SQLITE_BUSY，使 BEGIN IMMEDIATE
    # 的并发预约创建请求排队串行执行，而不是立即失败
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
