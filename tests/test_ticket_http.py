"""通过 ASGI 测试聊天、来源回执和工单路由；数据库使用临时 SQLite。"""
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

import api.main as api
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

    async def commit_turn(self, user, conv, key, message, response):
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
