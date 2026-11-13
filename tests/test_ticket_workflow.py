"""售后工单工作流测试，不调用外部LLM。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from domain.models import TicketState
from domain.repository import SupportRepository
from tooling.support_tools import SupportTools
from workflow.ticket_workflow import TicketWorkflow


class LocalToolManager:
    """只用于领域测试的轻量工具调度器，避免依赖LLM SDK。"""

    def __init__(self, support: SupportTools):
        self.handlers = {
            "get_order": support.get_order,
            "get_logistics": support.get_logistics,
            "check_refund_eligibility": support.check_refund_eligibility,
            "create_refund": support.create_refund,
            "get_refund_status": support.get_refund_status,
        }

    async def call(self, name, params, context=None, **kwargs):
        try:
            data = await self.handlers[name](params, context)
            return SimpleNamespace(success=True, data=data, error=None)
        except Exception as ex:
            return SimpleNamespace(success=False, data=None, error=str(ex))


class TicketWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp_dir.name) / "support.db")
        self.repository = SupportRepository(db_path)
        await self.repository.initialize()
        self.manager = LocalToolManager(SupportTools(self.repository))
        self.workflow = TicketWorkflow(
            self.repository,
            self.manager,
            approval_threshold=100.0,
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_high_risk_refund_requires_approval_and_is_idempotent(self):
        ticket = await self.workflow.create_ticket(
            user_id="user_001",
            message="请给订单A12345退款200元",
        )
        self.assertEqual(TicketState.WAITING_APPROVAL, ticket.state)
        self.assertIsNone(ticket.approved_by)

        # 即使知道工具参数，绕过工作流直接执行也会失败。
        bypass = await self.manager.call(
            "create_refund",
            dict(ticket.proposed_action),
            use_cache=False,
        )
        self.assertFalse(bypass.success)

        resolved = await self.workflow.approve(ticket.ticket_id, actor="manager_001")
        self.assertEqual(TicketState.RESOLVED, resolved.state)
        self.assertEqual("SUCCEEDED", resolved.result["status"])

        # 相同逻辑请求返回原工单，不会创建第二次退款。
        duplicate = await self.workflow.create_ticket(
            user_id="user_001",
            message="订单A12345请再次退款200元",
        )
        self.assertEqual(ticket.ticket_id, duplicate.ticket_id)
        self.assertEqual(TicketState.RESOLVED, duplicate.state)

        refund = await self.repository.get_refund_by_idempotency_key(
            ticket.proposed_action["idempotency_key"]
        )
        self.assertIsNotNone(refund)
        self.assertEqual(ticket.ticket_id, refund.ticket_id)

    async def test_low_risk_refund_executes_automatically(self):
        ticket = await self.workflow.create_ticket(
            user_id="demo_user",
            message="订单A10001退款50元",
        )
        self.assertEqual(TicketState.RESOLVED, ticket.state)
        self.assertIsNone(ticket.approved_by)
        self.assertEqual(50.0, ticket.result["amount"])

    async def test_missing_order_can_be_supplied_later(self):
        ticket = await self.workflow.create_ticket(
            user_id="demo_user",
            message="我想申请退款",
        )
        self.assertEqual(TicketState.COLLECTING_INFO, ticket.state)

        ticket = await self.workflow.supply_details(
            ticket.ticket_id,
            actor="demo_user",
            order_id="A10001",
            amount=60.0,
        )
        self.assertEqual(TicketState.RESOLVED, ticket.state)

        events = await self.workflow.get_audit(ticket.ticket_id)
        event_types = [event["event_type"] for event in events]
        self.assertIn("ticket.missing_information", event_types)
        self.assertIn("ticket.details_supplied", event_types)
        self.assertIn("refund.verified", event_types)

    async def test_rejected_ticket_cannot_be_approved(self):
        ticket = await self.workflow.create_ticket(
            user_id="user_001",
            message="订单A12345退款150元",
        )
        rejected = await self.workflow.reject(
            ticket.ticket_id,
            actor="manager_001",
            reason="证据不足",
        )
        self.assertEqual(TicketState.REJECTED, rejected.state)
        with self.assertRaises(ValueError):
            await self.workflow.approve(ticket.ticket_id, actor="manager_002")


if __name__ == "__main__":
    unittest.main()
