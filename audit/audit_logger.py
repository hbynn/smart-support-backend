"""结构化审计日志。

业务审计与普通应用日志不同：它记录谁在什么时间改变了哪张工单，
并持久化到数据库，便于排查审批绕过、重复执行等问题。
"""
from __future__ import annotations

from typing import Any, Dict, List

from domain.models import AuditEvent
from domain.repository import SupportRepository


class AuditLogger:
    def __init__(self, repository: SupportRepository):
        self._repository = repository

    async def record(
        self,
        ticket_id: str,
        event_type: str,
        *,
        actor: str,
        detail: Dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=None,
            ticket_id=ticket_id,
            event_type=event_type,
            actor=actor,
            detail=detail or {},
        )
        return await self._repository.add_audit_event(event)

    async def list_for_ticket(self, ticket_id: str) -> List[Dict[str, Any]]:
        events = await self._repository.list_audit_events(ticket_id)
        return [event.to_dict() for event in events]

