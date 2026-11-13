"""售后工单领域对象。

这里仅描述业务数据和状态，不依赖 FastAPI、LLM 或数据库实现。
把领域模型与基础设施解耦后，工作流可以独立测试，也便于将 SQLite
替换为 PostgreSQL 等生产存储。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
import uuid


def utc_now() -> str:
    """返回带时区的 ISO-8601 时间，避免本地时区造成审计歧义。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """生成适合演示和日志检索的短业务 ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class OrderStatus(str, Enum):
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class TicketState(str, Enum):
    CREATED = "CREATED"
    COLLECTING_INFO = "COLLECTING_INFO"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    HIGH = "HIGH"


class RefundStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class Order:
    order_id: str
    user_id: str
    amount: float
    status: OrderStatus
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Ticket:
    ticket_id: str
    user_id: str
    message: str
    request_key: str
    state: TicketState = TicketState.CREATED
    action_type: str = "refund"
    order_id: Optional[str] = None
    requested_amount: Optional[float] = None
    reason: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    proposed_action: Dict[str, Any] = field(default_factory=dict)
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    version: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["risk_level"] = self.risk_level.value
        data["approval_required"] = self.state == TicketState.WAITING_APPROVAL
        return data


@dataclass
class RefundRecord:
    refund_id: str
    ticket_id: str
    order_id: str
    user_id: str
    amount: float
    idempotency_key: str
    status: RefundStatus
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class AuditEvent:
    event_id: Optional[int]
    ticket_id: str
    event_type: str
    actor: str
    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

