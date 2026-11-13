"""订单和退款业务工具。

工具层负责业务校验和执行，不负责决定流程顺序。高风险退款即使绕过
Workflow 直接调用此处，也会再次检查工单状态与审批信息。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from domain.models import OrderStatus, RiskLevel, TicketState
from domain.repository import RepositoryError, SupportRepository

if TYPE_CHECKING:
    from tooling.tool_manager import MCPToolManager


class SupportTools:
    def __init__(self, repository: SupportRepository):
        self._repository = repository

    async def get_order(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        order_id = str(params.get("order_id", "")).strip()
        user_id = str(params.get("user_id", "")).strip()
        order = await self._repository.get_order(order_id)
        if order is None:
            raise ValueError(f"订单不存在: {order_id}")
        if user_id and order.user_id != user_id:
            raise PermissionError("订单不属于当前用户")
        return order.to_dict()

    async def get_logistics(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        order = await self.get_order(params, context)
        mapping = {
            OrderStatus.PAID.value: "已支付，等待发货",
            OrderStatus.SHIPPED.value: "运输中，预计1-3个工作日送达",
            OrderStatus.DELIVERED.value: "已签收",
            OrderStatus.REFUNDED.value: "已退款，物流任务已结束",
            OrderStatus.CANCELLED.value: "订单已取消",
        }
        return {
            "order_id": order["order_id"],
            "status": order["status"],
            "description": mapping.get(order["status"], "物流状态未知"),
        }

    async def check_refund_eligibility(
        self, params: Dict[str, Any], context: Any
    ) -> Dict[str, Any]:
        order = await self.get_order(params, context)
        amount = float(params.get("amount", order["amount"]))
        reasons = []
        if order["status"] in (OrderStatus.REFUNDED.value, OrderStatus.CANCELLED.value):
            reasons.append(f"订单状态为 {order['status']}")
        if amount <= 0:
            reasons.append("退款金额必须大于0")
        if amount > float(order["amount"]):
            reasons.append("退款金额超过订单金额")
        return {
            "eligible": not reasons,
            "order_id": order["order_id"],
            "requested_amount": round(amount, 2),
            "order_amount": order["amount"],
            "reasons": reasons,
        }

    async def create_refund(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        ticket_id = str(params.get("ticket_id", "")).strip()
        ticket = await self._repository.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")
        if ticket.state != TicketState.EXECUTING:
            raise RepositoryError("退款工具只能处理 EXECUTING 状态的工单")
        if ticket.risk_level == RiskLevel.HIGH and not ticket.approved_by:
            raise PermissionError("高风险退款未通过人工审批")

        expected = ticket.proposed_action
        order_id = str(params.get("order_id", ""))
        user_id = str(params.get("user_id", ""))
        amount = round(float(params.get("amount", 0)), 2)
        idempotency_key = str(params.get("idempotency_key", ""))
        if (
            order_id != expected.get("order_id")
            or user_id != ticket.user_id
            or amount != round(float(expected.get("amount", 0)), 2)
            or idempotency_key != expected.get("idempotency_key")
        ):
            raise PermissionError("退款参数与已规划并审批的操作不一致")

        refund, created = await self._repository.create_refund_once(
            ticket_id=ticket.ticket_id,
            order_id=order_id,
            user_id=user_id,
            amount=amount,
            idempotency_key=idempotency_key,
        )
        data = refund.to_dict()
        data["idempotent_replay"] = not created
        return data

    async def get_refund_status(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        key = str(params.get("idempotency_key", "")).strip()
        refund = await self._repository.get_refund_by_idempotency_key(key)
        return {
            "found": refund is not None,
            "refund": refund.to_dict() if refund else None,
        }


def register_support_tools(manager: "MCPToolManager", support: SupportTools) -> None:
    """把业务 Handler 注册到现有工具治理层。"""
    from tooling.tool_manager import Tool

    manager.register(Tool(
        name="get_order",
        description="根据订单号查询当前用户的订单",
        handler=support.get_order,
        schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "user_id": {"type": "string"}},
            "required": ["order_id", "user_id"],
        },
        cache_ttl=30.0,
        timeout_s=5.0,
    ))
    manager.register(Tool(
        name="get_logistics",
        description="查询订单物流状态",
        handler=support.get_logistics,
        schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "user_id": {"type": "string"}},
            "required": ["order_id", "user_id"],
        },
        cache_ttl=30.0,
        timeout_s=5.0,
    ))
    manager.register(Tool(
        name="check_refund_eligibility",
        description="检查订单和金额是否满足退款条件",
        handler=support.check_refund_eligibility,
        schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["order_id", "user_id", "amount"],
        },
        cache_ttl=0.0,
        timeout_s=5.0,
    ))
    manager.register(Tool(
        name="create_refund",
        description="执行已规划且已满足审批要求的退款",
        handler=support.create_refund,
        schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "amount": {"type": "number"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["ticket_id", "order_id", "user_id", "amount", "idempotency_key"],
        },
        cache_ttl=0.0,
        timeout_s=10.0,
    ))
    manager.register(Tool(
        name="get_refund_status",
        description="使用幂等键确认退款是否已经执行",
        handler=support.get_refund_status,
        schema={
            "type": "object",
            "properties": {"idempotency_key": {"type": "string"}},
            "required": ["idempotency_key"],
        },
        cache_ttl=0.0,
        timeout_s=5.0,
    ))
