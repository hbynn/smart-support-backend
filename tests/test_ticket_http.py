"""通过 ASGI 测试聊天、来源回执和工单路由；数据库使用临时 SQLite。"""
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import api.main as api
from agents.agent_orchestrator import AgentType, OrchestratorResult
from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
from domain.models import Ticket, TicketState
from domain.repository import SupportRepository
from test_ticket_workflow import LocalToolManager
from tooling.support_tools import SupportTools
from workflow.ticket_workflow import TicketWorkflow


class Memory:
    def __init__(self):
        self.receipts = {("demo_user", "yesterday", "q1"): {"message": "给订单申请退款"}}

    @asynccontextmanager
    async def conversation(self, *_):
        yield

    async def get_turn_receipt(self, user, conv, key):
        return self.receipts.get((user, conv, key))

    async def get_context(self, user, conv, *, query):
        return SimpleNamespace(recent_messages=[], to_prompt_text=lambda: "")

    async def commit_turn(self, user, conv, key, message, response, trace=None, *, state=None):
        self.receipts[user, conv, key] = {"message": message, "response": response}


class TicketHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_link_query_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SupportRepository(str(Path(tmp) / "support.db"))
            await repo.initialize()
            workflow = TicketWorkflow(repo, LocalToolManager(SupportTools(repo)))
            with patch.multiple(api, _orchestrator=object(), _memory=Memory(),
                                _support_repository=repo, _ticket_workflow=workflow), patch.dict(
                                    "os.environ", {"ECHOMIND_API_TOKENS": ""}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                             base_url="http://test") as client:
                    create = {"message": "订单 A10001 退款 50 元", "user_id": "demo_user",
                              "source_conv_id": "yesterday", "source_request_id": "bad"}
                    self.assertEqual((await client.post("/tickets", json=create)).status_code, 400)
                    create["source_request_id"] = "q1"
                    created = await client.post("/tickets", json=create)
                    self.assertEqual(created.status_code, 200, created.text)
                    chat = {"message": "上次那笔退款怎么样了", "user_id": "demo_user",
                            "conv_id": "new", "client_request_id": "q2"}
                    result = await client.post("/chat", json=chat)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertIn(created.json()["ticket_id"], result.json()["response"])
                    self.assertEqual((await client.post("/chat", json=chat)).json(), result.json())

    async def test_refund_status_and_invoice_share_one_chat_turn(self):
        await self._assert_refund_status_and_invoice("发票补开需要什么？")

    async def test_invoice_date_does_not_filter_out_old_refund_ticket(self):
        await self._assert_refund_status_and_invoice("今天补开订单 A10001 的发票需要什么？")

    async def _assert_refund_status_and_invoice(self, invoice_question):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SupportRepository(str(Path(tmp) / "support.db"))
            await repository.initialize()
            ticket = Ticket(
                ticket_id="ticket_123456789abc",
                user_id="demo_user",
                message="申请订单 A10002 退款 150 元",
                request_key="pending-refund",
                order_id="A10002",
                requested_amount=150,
                state=TicketState.WAITING_APPROVAL,
                created_at="2000-01-01T00:00:00+00:00",
            )
            await repository.create_ticket(ticket)
            invoice_answer = "补开发票请提供订单号和原发票号码。"
            recognize_intent = AsyncMock(return_value=IntentResult(
                IntentCategory.INVOICE, 1.0, UrgencyLevel.LOW, "billing", {}, "test", 0,
            ))

            async def invoice_agent(request):
                return OrchestratorResult(
                    request_id=request.request_id,
                    response=invoice_answer,
                    agent_type=AgentType.BILLING,
                    intent=IntentCategory.INVOICE,
                )

            orchestrator = SimpleNamespace(recognize_intent=recognize_intent,
                                           run=AsyncMock(side_effect=invoice_agent),
                                           get_tool_trace=Mock(return_value=None))
            memory = Memory()
            chat = {
                "message": f"查一下 A10002 的退款进度，另外{invoice_question}",
                "user_id": "demo_user",
                "conv_id": "compound",
                "client_request_id": "invoice-and-refund",
            }
            with patch.multiple(api, _orchestrator=orchestrator, _memory=memory,
                                _support_repository=repository, _profile_updater=None), patch.dict(
                                    "os.environ", {"ECHOMIND_ENV": "development", "ECHOMIND_API_TOKENS": ""}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                             base_url="http://test") as client:
                    result = await client.post("/chat", json=chat)
                    self.assertEqual(result.status_code, 200, result.text)
                    reply = result.json()
                    self.assertEqual(reply["status"], "success")
                    self.assertIn(ticket.ticket_id, reply["response"])
                    self.assertIn("等待人工审批", reply["response"])
                    self.assertIn(invoice_answer, reply["response"])
                    self.assertIn("get_my_ticket_status", reply["tools_used"])
                    self.assertEqual((await client.post("/chat", json=chat)).json(), reply)

            recognize_intent.assert_awaited_once()
            orchestrator.run.assert_awaited_once()
            normalized_invoice = invoice_question.rstrip("？?")
            self.assertEqual(recognize_intent.await_args.args[0].rstrip("？?"), normalized_invoice)
            self.assertEqual(orchestrator.run.await_args.args[0].message.rstrip("？?"), normalized_invoice)
            receipt = await memory.get_turn_receipt(chat["user_id"], chat["conv_id"], chat["client_request_id"])
            self.assertEqual(receipt["message"], chat["message"])
            self.assertEqual((await repository.get_ticket(ticket.ticket_id)).state, TicketState.WAITING_APPROVAL)
            with closing(sqlite3.connect(repository.db_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM refunds").fetchone(), (0,))
                self.assertEqual(db.execute("SELECT request_id, ticket_id FROM ticket_mentions").fetchall(),
                                 [(chat["client_request_id"], ticket.ticket_id)])
