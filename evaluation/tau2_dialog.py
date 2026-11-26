"""Replay a fixed user script against EchoMind through the τ-Bench orchestrator."""

import os
from uuid import uuid4

# τ-Bench uses its local model cost map even when the simulated user is scripted.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

from tau2.agent.base_agent import HalfDuplexAgent
from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.tasks import Task, UserScenario
from tau2.orchestrator.orchestrator import Orchestrator
from tau2.runner import build_environment
from tau2.user.user_simulator_base import HalfDuplexUser


class FixedUser(HalfDuplexUser):
    def __init__(self, messages):
        super().__init__()
        self.messages = [*messages, "###STOP###"]

    def get_init_state(self, message_history=None):
        return 0

    def generate_next_message(self, message, state):
        return UserMessage.text(self.messages[state]), state + 1


class EchoMindAgent(HalfDuplexAgent):
    def __init__(self, client, user_id):
        super().__init__(tools=[], domain_policy="")
        self.client = client
        self.user_id = user_id
        self.conv_id = f"tau-{uuid4().hex}"
        self.replies = {}

    def get_init_state(self, message_history=None):
        return 0

    def generate_next_message(self, message, state):
        response = self.client.post(
            "/chat",
            json={
                "user_id": self.user_id,
                "conv_id": self.conv_id,
                "client_request_id": f"{self.conv_id}-{state}",
                "message": message.content,
            },
        )
        # Keep application errors in the report too, before the orchestrator stops.
        self.replies[state] = response.json()
        response.raise_for_status()
        return AssistantMessage.text(self.replies[state]["response"]), state + 1


def run_dialog(agent, *, case_id, instructions, messages):
    return Orchestrator(
        domain="mock",
        agent=agent,
        user=FixedUser(messages),
        environment=build_environment("mock"),
        task=Task(
            id=case_id,
            user_scenario=UserScenario(instructions=instructions),
            agent_opener="你好，请问有什么可以帮你？",
        ),
        max_steps=2 * len(messages) + 4,
    ).run()
