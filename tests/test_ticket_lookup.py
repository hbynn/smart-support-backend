"""工单场景在不同会话中定位、澄清和校验归属。"""
import tempfile
import unittest
from pathlib import Path

from domain.repository import SupportRepository
from domain.models import Ticket, TicketState
from workflow.ticket_lookup import status_answer


class TicketLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SupportRepository(str(Path(tmp) / "support.db"))
            await repo.initialize()
            first = Ticket(ticket_id="ticket_111111111111", user_id="u", order_id="A12345",
                           request_key="one", message="退款", state=TicketState.WAITING_APPROVAL)
            await repo.create_ticket(first)
            await repo.link_ticket("u", "old", "one", first.ticket_id)
            await repo.link_ticket("u", "old", "one", first.ticket_id)
            text, key = await status_answer(repo, "u", "new", "上次那笔退款怎么样了")
            self.assertEqual(key, first.ticket_id)
            self.assertIn("等待人工审批", text)
            first.state = TicketState.RESOLVED
            await repo.save_ticket(first)
            self.assertIn("已处理完成", (await status_answer(repo, "u", "new", "那笔退款进度"))[0])
            await repo.create_ticket(Ticket(ticket_id="ticket_222222222222", user_id="u", order_id="A10002",
                                            request_key="two", message="退款"))
            self.assertIsNone((await status_answer(repo, "u", "new", "那笔退款进度"))[1])
            self.assertEqual((await status_answer(repo, "u", "old", "那笔退款进度"))[1], first.ticket_id)
            self.assertIn("没有查到", (await status_answer(repo, "other", "old", first.ticket_id))[0])
            self.assertIsNone(await status_answer(repo, "u", "old", "物流订单 A12345 状态如何"))
