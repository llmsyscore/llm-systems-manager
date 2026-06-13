"""Metric data models."""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .._time import now_utc


class MetricPoint(BaseModel):
    """A single metric data point."""
    metric_id: UUID = Field(default_factory=uuid4)
    source: str = Field(description="Metric source (gpu, cpu, ram, disk, network, psu)")
    metric_name: str = Field(description="Metric name (temperature, vram_usage, cpu_usage, etc.)")
    value: float = Field(description="Metric value")
    unit: Optional[str] = Field(default=None, description="Metric unit (°C, %, Mbps, etc.)")
    timestamp: datetime = Field(default_factory=now_utc)
    hostname: Optional[str] = Field(default=None, description="Server/device that produced the metric")

    def to_dict(self) -> dict:
        """Convert to plain dictionary for InfluxDB storage."""
        return {
            "metric_id": str(self.metric_id),
            "source": self.source,
            "metric_name": self.metric_name,
            "value": self.value,
            "unit": self.unit,
            "timestamp": self.timestamp.isoformat(),
            "hostname": self.hostname,
        }


class MetricSummary(BaseModel):
    """Aggregated metric summary."""
    source: str
    metric_name: str
    unit: Optional[str]
    current_value: float
    min_value: float
    max_value: float
    avg_value: float
    std_dev: float
    p90: float
    p95: float
    p99: float
    data_points: int
    last_updated: datetime

    def to_dict(self) -> dict:
        """Convert to plain dictionary."""
        return {
            "source": self.source,
            "metric_name": self.metric_name,
            "unit": self.unit,
            "current_value": self.current_value,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "avg_value": self.avg_value,
            "std_dev": self.std_dev,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "data_points": self.data_points,
            "last_updated": self.last_updated.isoformat(),
        }


class MetricHistoryResponse(BaseModel):
    """Response for metric historical data."""
    source: str
    metric_name: str
    unit: Optional[str]
    points: list[MetricPoint]
    statistics: Optional[dict] = None
    trend: Optional[str] = None  # "increasing", "decreasing", "stable"


class MetricBatchCreate(BaseModel):
    """Batch of metric points for bulk ingestion."""
    metrics: list[MetricPoint] = Field(min_length=1, description="List of metric points")