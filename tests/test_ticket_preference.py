"""用同一用户的两轮明确声明，验证画像覆盖与来源回溯。"""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from memory.conversation_memory import MemoryManager


class PreferenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_correction(self):
        memory = MemoryManager.__new__(MemoryManager)
        memory._redis = MagicMock()
        pipe = MagicMock()
        pipe.execute = AsyncMock()
        memory._redis.pipeline.return_value.__aenter__.return_value = pipe
        memory._migrate_legacy_keys = AsyncMock()
        data = {}
        pipe.set.side_effect = data.__setitem__
        for k, text in enumerate(["以后简短回答", "今后请详细解释", "用户说过以后简短回答"]):
            await memory.commit_turn("u", "c", str(k), text, {"response": "收到"})
        value = json.loads(next(iter(data.values())))
        self.assertEqual((value["value"], value["request_id"]), ("详细解释", "1"))
        memory._redis.get = AsyncMock(side_effect=data.get)
        memory._get_working_memory = AsyncMock(return_value=[])
        memory._search_episodic = AsyncMock(return_value=[])
        memory._get_profile = AsyncMock(return_value={})
        ctx = await memory.get_context("u", "other")
        self.assertEqual(ctx.user_profile["explicit_reply_style"], "详细解释")
