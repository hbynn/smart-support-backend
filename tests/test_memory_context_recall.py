"""Regression tests for recalling an earlier user message into the Agent prompt."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.performance import collect_performance
from memory.conversation_memory import (
    EARLIER_MESSAGE_MAX_CHARS,
    MemoryManager,
    Message,
    MsgRole,
)


class MemoryContextRecallTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.query = "请复述浏览器配置代号。"
        self.earlier = Message(MsgRole.USER, "浏览器配置代号为 QZ7M-K4PX。")
        self.recent = [
            Message(MsgRole.USER if index % 2 == 0 else MsgRole.ASSISTANT,
                    f"无关对话 {index}")
            for index in range(8)
        ]

    async def test_context_contains_recalled_message_and_all_eight_recent_messages(self):
        memory = MemoryManager.__new__(MemoryManager)
        memory._get_working_memory = AsyncMock(return_value=[self.earlier, *self.recent])
        memory._search_episodic = AsyncMock(return_value=[])
        memory._get_profile = AsyncMock(return_value={})
        memory._redis = SimpleNamespace(get=AsyncMock(return_value=None))

        with collect_performance() as trace:
            context = await memory.get_context("test-user", "test-conversation", self.query)

        self.assertEqual(context.earlier_user_message, self.earlier.content)
        self.assertEqual(trace["events"], {"working_memory_earlier_message_recalled": 1})
        recalled, recent = context.to_prompt_text().split("[最近对话]\n\n")
        self.assertIn(f"user: {self.earlier.content}", recalled)
        self.assertEqual(
            recent.split("\n\n"),
            [f"{message.role.value}: {message.content}" for message in self.recent],
        )

    def test_recent_correction_wins_a_relevance_tie(self):
        correction = Message(MsgRole.USER, "浏览器配置代号改为 V2T9-HG4S。")
        messages = [self.earlier, *self.recent[:-1], correction]

        self.assertEqual(MemoryManager._recall_earlier_user_message(messages, self.query), "")

    def test_excludes_old_assistant_oversized_and_unrelated_messages(self):
        candidates = {
            "assistant": Message(MsgRole.ASSISTANT, self.earlier.content),
            "oversized": Message(
                MsgRole.USER,
                self.earlier.content.ljust(EARLIER_MESSAGE_MAX_CHARS + 1, "甲"),
            ),
            "unrelated": Message(MsgRole.USER, "请解释闰年二月有多少天。"),
        }

        for name, candidate in candidates.items():
            with self.subTest(name=name):
                messages = [candidate, *self.recent]
                self.assertEqual(MemoryManager._recall_earlier_user_message(messages, self.query), "")
