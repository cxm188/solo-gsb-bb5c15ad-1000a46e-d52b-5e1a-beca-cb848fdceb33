# 港口潮汐通航窗口 API

基于 **Python + FastAPI + SQLite** 的港口潮汐通航窗口计算服务。

- 录入港口的**潮高预报点**与**航道基准水深**；
- 船舶提交**吃水**、**安全富余**和**正数通航时长**；
- 相邻预报点间按**线性插值**求潮高，仅在**预报覆盖时段**内计算
  `水深 = 基准水深 + 潮高 ≥ 吃水 + 安全富余` 的连续达标窗口（**端点恰好达标也纳入**）；
- 每个达标窗口的**结束时刻减去通航时长**得到**可出发时段**，不足所需时长的窗口舍弃；
- 返回**全部可出发时段**和**最早出发时刻**；
- 预报点更正（同一时刻重复提交即覆盖）后，查询自动按最新数据实时重算。

## 快速开始（单条命令）

```bash
docker compose up --build
```

该命令自动完成：依赖安装 → SQLite 数据库初始化（服务启动时幂等建表）→ 启动服务。

- **API 地址**：http://localhost:8000
- **交互式文档（Swagger UI）**：http://localhost:8000/docs
- **健康检查**：http://localhost:8000/health

停止：`docker compose down`（加 `-v` 可同时删除数据卷）。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 服务端口 | `8000` | 修改 `docker-compose.yml` 中 `ports` 左侧的宿主机端口即可 |
| `DATABASE_PATH` | `/data/tide.db`（容器内）/ `tide.db`（本地） | SQLite 数据库文件路径，通过环境变量配置 |
| 数据持久化 | 命名卷 `tide-data` → `/data` | 容器重建后数据保留 |

时间约定：所有时刻使用 ISO 8601；不带时区信息时按 **UTC** 处理，响应统一返回 UTC 时刻。
长度单位为米，时长单位为分钟。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/ports` | 录入港口（名称 + 航道基准水深） |
| `GET` | `/ports` / `/ports/{id}` | 查询港口 |
| `PATCH` | `/ports/{id}` | 更正港口名称 / 基准水深 |
| `PUT` | `/ports/{id}/tide-forecasts` | 批量录入/更正潮高预报点（同一时刻重复提交即覆盖） |
| `GET` | `/ports/{id}/tide-forecasts` | 查看当前预报点 |
| `POST` | `/ports/{id}/windows` | 提交吃水、安全富余、通航时长，计算可出发时段 |

### 完整示例

```bash
# 1. 录入港口：航道基准水深 4.5 米
curl -X POST http://localhost:8000/ports \
  -H "Content-Type: application/json" \
  -d '{"name": "示例港", "reference_depth": 4.5}'

# 2. 录入潮高预报点（每 3 小时一个点）
curl -X PUT http://localhost:8000/ports/1/tide-forecasts \
  -H "Content-Type: application/json" \
  -d '{"points": [
        {"forecast_time": "2026-09-24T00:00:00Z", "tide_height": 0.5},
        {"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 3.0},
        {"forecast_time": "2026-09-24T06:00:00Z", "tide_height": 0.5}
      ]}'

# 3. 船舶查询：吃水 5.0 米，安全富余 0.5 米，需要 60 分钟连续通航
curl -X POST http://localhost:8000/ports/1/windows \
  -H "Content-Type: application/json" \
  -d '{"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60}'
```

响应（所需水深 = 5.0 + 0.5 = 5.5 米；水深 5.0→7.5→5.0，线性插值得达标窗口
`[00:36, 05:24]`，减去 60 分钟通航时长）：

```json
{
  "port_id": 1,
  "required_depth": 5.5,
  "duration_minutes": 60.0,
  "earliest_departure": "2026-09-24T00:36:00+00:00",
  "windows": [
    {"start": "2026-09-24T00:36:00+00:00", "end": "2026-09-24T04:24:00+00:00"}
  ]
}
```

```bash
# 4. 更正 03:00 的预报点（3.0 → 1.0），随后查询自动按最新数据重算
curl -X PUT http://localhost:8000/ports/1/tide-forecasts \
  -H "Content-Type: application/json" \
  -d '{"points": [{"forecast_time": "2026-09-24T03:00:00Z", "tide_height": 1.0}]}'

curl -X POST http://localhost:8000/ports/1/windows \
  -H "Content-Type: application/json" \
  -d '{"draft": 5.0, "safety_margin": 0.5, "duration_minutes": 60}'
# → {"windows": [], "earliest_departure": null, ...}（峰值水深仅 5.5，窗口长度不足 60 分钟）
```

### 参数校验

- `duration_minutes` 必须为**正数**（`> 0`），否则返回 422；
- `draft`、`safety_margin` 必须为非负数；
- 预报点不足 2 个（无插值区间）或全程不达标时，返回空窗口列表与 `earliest_departure: null`。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # 运行依赖 + pytest/httpx
uvicorn app.main:app --reload         # 默认使用 ./tide.db，可用 DATABASE_PATH 覆盖
pytest                                # 运行全部测试（核心算法 + API 端到端）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期（启动时初始化数据库）
  tides.py      # 核心算法：线性插值、达标窗口、可出发时段
  database.py   # SQLite 连接与幂等建表
  schemas.py    # Pydantic 请求/响应模型与校验
tests/
  test_tides.py # 核心算法单元测试（仅标准库）
  test_api.py   # API 端到端测试
Dockerfile / docker-compose.yml / requirements*.txt
```
