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
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
