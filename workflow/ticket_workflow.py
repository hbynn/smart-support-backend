"""退款工单状态机。

LLM 负责理解用户，但有副作用的业务动作必须由确定性工作流控制。
这个模块负责状态转换、风险审批、幂等执行和结果确认。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional

from audit.audit_logger import AuditLogger
from domain.models import RiskLevel, Ticket, TicketState, new_id, utc_now
from domain.repository import SupportRepository


class TicketNotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


class TicketWorkflow:
    ORDER_RE = re.compile(
        r"(?:订单号?|order(?:_id)?|#)\s*[:：#]?\s*([A-Za-z0-9_-]{4,32})",
        re.I,
    )
    AMOUNT_RE = re.compile(
        r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块|rmb|cny)?",
        re.I,
    )

    def __init__(
        self,
        repository: SupportRepository,
        tool_manager: Any,
        *,
        approval_threshold: float = 100.0,
    ):
        self._repository = repository
        self._tools = tool_manager
        self._audit = AuditLogger(repository)
        self._approval_threshold = approval_threshold

    async def create_ticket(
        self,
        *,
        user_id: str,
        message: str,
        order_id: Optional[str] = None,
        amount: Optional[float] = None,
        reason: str = "",
    ) -> Ticket:
        user_id = user_id.strip()
        message = message.strip()
        order_id = (order_id or self._extract_order_id(message) or "").strip() or None
        amount = amount if amount is not None else self._extract_amount(message, order_id)

        # 如果订单明确且属于当前用户，未给金额时按全额退款规划。
        if order_id and amount is None:
            order = await self._repository.get_order(order_id)
            if order and order.user_id == user_id:
                amount = order.amount

        request_key = self._request_key(user_id, order_id, amount, message)
        ticket = Ticket(
            ticket_id=new_id("ticket"),
            user_id=user_id,
            message=message,
            request_key=request_key,
            order_id=order_id,
            requested_amount=round(float(amount), 2) if amount is not None else None,
            reason=reason or message,
        )
        ticket, created = await self._repository.create_ticket(ticket)
        if not created:
            await self._audit.record(
                ticket.ticket_id,
                "ticket.duplicate_request",
                actor=user_id,
                detail={"request_key": request_key},
            )
            return ticket

        await self._audit.record(
            ticket.ticket_id,
            "ticket.created",
            actor=user_id,
            detail={"order_id": order_id, "amount": amount},
        )
        if not order_id:
            ticket.state = TicketState.COLLECTING_INFO
            ticket.error = "缺少订单号"
            await self._repository.save_ticket(ticket)
            await self._audit.record(
                ticket.ticket_id,
                "ticket.missing_information",
                actor="system",
                detail={"missing": ["order_id"]},
            )
            return ticket
        return await self._plan(ticket)

    async def supply_details(
        self,
        ticket_id: str,
        *,
        actor: str,
        order_id: str,
        amount: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> Ticket:
        ticket = await self._require_ticket(ticket_id)
        if ticket.state != TicketState.COLLECTING_INFO:
            raise InvalidTransition(f"状态 {ticket.state.value} 不接受补充信息")
        order = await self._repository.get_order(order_id)
        ticket.order_id = order_id
        ticket.requested_amount = round(float(amount if amount is not None else order.amount), 2) if order else amount
        if reason:
            ticket.reason = reason
        ticket.error = None
        ticket.state = TicketState.PLANNING
        await self._repository.save_ticket(ticket)
        await self._audit.record(
            ticket.ticket_id,
            "ticket.details_supplied",
            actor=actor,
            detail={"order_id": order_id, "amount": ticket.requested_amount},
        )
        return await self._plan(ticket)

    async def approve(self, ticket_id: str, *, actor: str) -> Ticket:
        ticket = await self._require_ticket(ticket_id)
        if ticket.state != TicketState.WAITING_APPROVAL:
            raise InvalidTransition(f"状态 {ticket.state.value} 不能审批")
        ticket.approved_by = actor
        ticket.approved_at = utc_now()
        ticket.state = TicketState.EXECUTING
        ticket.error = None
        await self._repository.save_ticket(ticket)
        await self._audit.record(
            ticket.ticket_id,
            "ticket.approved",
            actor=actor,
            detail={"action": ticket.proposed_action},
        )
        return await self._execute_refund(ticket)

    async def reject(self, ticket_id: str, *, actor: str, reason: str) -> Ticket:
        ticket = await self._require_ticket(ticket_id)
        if ticket.state != TicketState.WAITING_APPROVAL:
            raise InvalidTransition(f"状态 {ticket.state.value} 不能拒绝")
        ticket.state = TicketState.REJECTED
        ticket.error = reason or "人工拒绝"
        await self._repository.save_ticket(ticket)
        await self._audit.record(
            ticket.ticket_id,
            "ticket.rejected",
            actor=actor,
            detail={"reason": ticket.error},
        )
        return ticket

    async def retry(self, ticket_id: str, *, actor: str) -> Ticket:
        ticket = await self._require_ticket(ticket_id)
        if ticket.state == TicketState.RESOLVED:
            return ticket
        if ticket.state not in (TicketState.FAILED, TicketState.VERIFYING, TicketState.EXECUTING):
            raise InvalidTransition(f"状态 {ticket.state.value} 不能重试")

        # 未知结果先按幂等键查询，绝不直接再次创建退款。
        verified = await self._verify_refund(ticket, actor=actor)
        if verified.state == TicketState.RESOLVED:
            return verified
        if ticket.risk_level == RiskLevel.HIGH and not ticket.approved_by:
            ticket.state = TicketState.WAITING_APPROVAL
            await self._repository.save_ticket(ticket)
            return ticket

        ticket.state = TicketState.EXECUTING
        ticket.error = None
        await self._repository.save_ticket(ticket)
        await self._audit.record(ticket.ticket_id, "ticket.retry", actor=actor)
        return await self._execute_refund(ticket)

    async def get_ticket(self, ticket_id: str) -> Ticket:
        return await self._require_ticket(ticket_id)

    async def get_audit(self, ticket_id: str) -> list[Dict[str, Any]]:
        await self._require_ticket(ticket_id)
        return await self._audit.list_for_ticket(ticket_id)

    async def _plan(self, ticket: Ticket) -> Ticket:
        ticket.state = TicketState.PLANNING
        ticket.error = None
        await self._repository.save_ticket(ticket)

        order_result = await self._tools.call(
            "get_order",
            {"order_id": ticket.order_id, "user_id": ticket.user_id},
        )
        if not order_result.success:
            return await self._fail(ticket, "订单查询失败", order_result.error)

        if ticket.requested_amount is None:
            ticket.requested_amount = float(order_result.data["amount"])

        eligibility = await self._tools.call(
            "check_refund_eligibility",
            {
                "order_id": ticket.order_id,
                "user_id": ticket.user_id,
                "amount": ticket.requested_amount,
            },
            use_cache=False,
        )
        if not eligibility.success:
            return await self._fail(ticket, "退款资格检查失败", eligibility.error)
        if not eligibility.data.get("eligible"):
            return await self._fail(
                ticket,
                "订单不满足退款条件",
                "; ".join(eligibility.data.get("reasons", [])),
            )

        ticket.risk_level = (
            RiskLevel.HIGH
            if float(ticket.requested_amount) >= self._approval_threshold
            else RiskLevel.LOW
        )
        idempotency_key = f"refund:{ticket.ticket_id}"
        ticket.proposed_action = {
            "tool": "create_refund",
            "ticket_id": ticket.ticket_id,
            "order_id": ticket.order_id,
            "user_id": ticket.user_id,
            "amount": ticket.requested_amount,
            "idempotency_key": idempotency_key,
        }
        await self._audit.record(
            ticket.ticket_id,
            "ticket.planned",
            actor="system",
            detail={"risk_level": ticket.risk_level.value, "action": ticket.proposed_action},
        )

        if ticket.risk_level == RiskLevel.HIGH:
            ticket.state = TicketState.WAITING_APPROVAL
            await self._repository.save_ticket(ticket)
            await self._audit.record(
                ticket.ticket_id,
                "ticket.waiting_approval",
                actor="system",
                detail={"threshold": self._approval_threshold},
            )
            return ticket

        ticket.state = TicketState.EXECUTING
        await self._repository.save_ticket(ticket)
        return await self._execute_refund(ticket)

    async def _execute_refund(self, ticket: Ticket) -> Ticket:
        result = await self._tools.call(
            "create_refund",
            dict(ticket.proposed_action),
            use_cache=False,
        )
        ticket.state = TicketState.VERIFYING
        if result.success:
            ticket.result = dict(result.data)
            ticket.error = None
            await self._audit.record(
                ticket.ticket_id,
                "refund.command_accepted",
                actor="system",
                detail=ticket.result,
            )
        else:
            # 超时不等于失败，可能下游已执行成功，因此进入 VERIFYING。
            ticket.error = result.error or "退款执行结果未知"
            await self._audit.record(
                ticket.ticket_id,
                "refund.command_unknown",
                actor="system",
                detail={"error": ticket.error},
            )
        await self._repository.save_ticket(ticket)
        return await self._verify_refund(ticket, actor="system")

    async def _verify_refund(self, ticket: Ticket, *, actor: str) -> Ticket:
        key = ticket.proposed_action.get("idempotency_key")
        if not key:
            return await self._fail(ticket, "缺少退款幂等键")
        status = await self._tools.call(
            "get_refund_status",
            {"idempotency_key": key},
            use_cache=False,
        )
        if status.success and status.data.get("found"):
            ticket.state = TicketState.RESOLVED
            ticket.result = status.data["refund"]
            ticket.error = None
            await self._repository.save_ticket(ticket)
            await self._audit.record(
                ticket.ticket_id,
                "refund.verified",
                actor=actor,
                detail=ticket.result,
            )
            return ticket

        ticket.state = TicketState.FAILED if status.success else TicketState.VERIFYING
        ticket.error = (
            "未查询到退款结果，可以使用同一幂等键安全重试"
            if status.success
            else status.error or "退款状态查询失败"
        )
        await self._repository.save_ticket(ticket)
        await self._audit.record(
            ticket.ticket_id,
            "refund.verification_failed",
            actor=actor,
            detail={"error": ticket.error, "state": ticket.state.value},
        )
        return ticket

    async def _fail(self, ticket: Ticket, message: str, detail: Optional[str] = None) -> Ticket:
        ticket.state = TicketState.FAILED
        ticket.error = f"{message}: {detail}" if detail else message
        await self._repository.save_ticket(ticket)
        await self._audit.record(
            ticket.ticket_id,
            "ticket.failed",
            actor="system",
            detail={"error": ticket.error},
        )
        return ticket

    async def _require_ticket(self, ticket_id: str) -> Ticket:
        ticket = await self._repository.get_ticket(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"工单不存在: {ticket_id}")
        return ticket

    @classmethod
    def _extract_order_id(cls, message: str) -> Optional[str]:
        match = cls.ORDER_RE.search(message)
        return match.group(1) if match else None

    @classmethod
    def _extract_amount(cls, message: str, order_id: Optional[str]) -> Optional[float]:
        # 先移除订单号，避免把 A12345 中的数字误判成退款金额。
        text = message.replace(order_id, "") if order_id else message
        matches = list(cls.AMOUNT_RE.finditer(text))
        explicit = [m for m in matches if any(unit in m.group(0).lower() for unit in ("元", "块", "rmb", "cny", "¥", "￥"))]
        if not explicit:
            return None
        return float(explicit[0].group(1))

    @staticmethod
    def _request_key(
        user_id: str,
        order_id: Optional[str],
        amount: Optional[float],
        message: str,
    ) -> str:
        logical_target = order_id or " ".join(message.lower().split())
        amount_text = f"{float(amount):.2f}" if amount is not None else "full"
        raw = f"{user_id}|refund|{logical_target}|{amount_text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

