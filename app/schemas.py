"""Pydantic 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, Field


class PortCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="港口名称")
    reference_depth: float = Field(description="航道基准水深（米）")


class PortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    reference_depth: float | None = Field(default=None, description="航道基准水深（米）")


class PortOut(BaseModel):
    id: int
    name: str
    reference_depth: float


class TideForecastPoint(BaseModel):
    forecast_time: datetime = Field(description="预报时刻（ISO 8601；无时区信息按 UTC 处理）")
    tide_height: float = Field(description="潮高（米，相对海图基准面）")


class TideForecastUpsert(BaseModel):
    """批量录入/更正预报点：同一港口同一时刻重复提交即覆盖（更正）。"""

    points: list[TideForecastPoint] = Field(min_length=1)


class TideForecastOut(BaseModel):
    forecast_time: datetime
    tide_height: float


class WindowQuery(BaseModel):
    draft: float = Field(ge=0, description="船舶吃水（米）")
    safety_margin: float = Field(ge=0, description="安全富余（米）")
    duration_minutes: float = Field(gt=0, description="所需通航时长（分钟），必须为正数")


class DepartureWindow(BaseModel):
    start: datetime = Field(description="可出发时段起点")
    end: datetime = Field(description="可出发时段终点（达标窗口结束时刻 − 通航时长）")


class WindowQueryResult(BaseModel):
    port_id: int
    required_depth: float = Field(description="所需水深 = 吃水 + 安全富余（米）")
    duration_minutes: float
    earliest_departure: datetime | None = Field(description="最早可出发时刻；无可出发时段时为 null")
    windows: list[DepartureWindow] = Field(description="全部可出发时段")
