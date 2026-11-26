"""退款与工单审计 Outbox 的事务及投递回归。"""

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from domain.models import OrderStatus, Ticket, TicketState
from domain.repository import SupportRepository
from test_ticket_workflow import LocalToolManager
from tooling.support_tools import SupportTools
from workflow.ticket_workflow import TicketWorkflow


def sql(repository, statement, params=()):
    # A separate SQLite connection sees only committed writes and never runs the publisher.
    with closing(sqlite3.connect(repository.db_path)) as connection, connection:
        return connection.execute(statement, params).fetchall()


def audit_types(repository, ticket_id, *, pending=False):
    query = (
        "SELECT event_type FROM audit_outbox WHERE ticket_id = ? ORDER BY outbox_id"
        if pending
        else "SELECT event_type FROM audit_events WHERE ticket_id = ? ORDER BY event_id"
    )
    return [event_type for (event_type,) in sql(repository, query, (ticket_id,))]


async def prepare_refund(repository):
    ticket = Ticket(
        ticket_id="ticket_outbox",
        user_id="demo_user",
        message="订单 A10001 退款 50 元",
        request_key="request_outbox",
        order_id="A10001",
        requested_amount=50.0,
        state=TicketState.EXECUTING,
    )
    await repository.create_ticket(ticket)
    return {
        "ticket_id": ticket.ticket_id,
        "order_id": ticket.order_id,
        "user_id": ticket.user_id,
        "amount": ticket.requested_amount,
        "idempotency_key": f"refund:{ticket.ticket_id}",
    }


def refund_workflow(repository):
    return TicketWorkflow(repository, LocalToolManager(SupportTools(repository)))


class AuditOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.repository = SupportRepository(str(Path(temp_dir.name) / "support.db"))
        await self.repository.initialize()

    async def test_refund_audit_survives_repository_restart_and_publishes_once(self):
        repository = self.repository
        command = await prepare_refund(repository)
        ticket_id = command["ticket_id"]
        attempts = await asyncio.gather(
            repository.create_refund_once(**command), repository.create_refund_once(**command)
        )
        assert sorted(created for _, created in attempts) == [False, True]
        assert attempts[0][0].refund_id == attempts[1][0].refund_id
        assert audit_types(repository, ticket_id, pending=True) == ["refund.command_accepted"]
        assert audit_types(repository, ticket_id) == []

        restarted = SupportRepository(str(repository.db_path))
        await restarted.initialize()
        # Failing the materialization must leave the committed outbox row available for retry.
        sql(restarted, """
            CREATE TRIGGER fail_publish BEFORE INSERT ON audit_events
            BEGIN SELECT RAISE(ABORT, 'injected audit publish failure'); END
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected audit publish failure"):
            await restarted.publish_audit_events()
        assert audit_types(restarted, ticket_id, pending=True) == ["refund.command_accepted"]
        assert audit_types(restarted, ticket_id) == []
        sql(restarted, "DROP TRIGGER fail_publish")

        published = await asyncio.gather(
            restarted.publish_audit_events(), repository.publish_audit_events()
        )
        assert sorted(published) == [0, 1]
        refund, created = await restarted.create_refund_once(**command)
        assert not created
        assert refund.refund_id == attempts[0][0].refund_id
        assert await restarted.publish_audit_events() == 0
        assert audit_types(restarted, ticket_id, pending=True) == []
        assert audit_types(restarted, ticket_id) == ["refund.command_accepted"]

    async def test_outbox_insert_failure_rolls_back_refund_and_order(self):
        repository = self.repository
        command = await prepare_refund(repository)
        sql(repository, """
            CREATE TRIGGER fail_refund_audit BEFORE INSERT ON audit_outbox
            WHEN NEW.event_type = 'refund.command_accepted'
            BEGIN SELECT RAISE(ABORT, 'injected refund outbox failure'); END
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected refund outbox failure"):
            await repository.create_refund_once(**command)

        assert await repository.get_refund_by_idempotency_key(command["idempotency_key"]) is None
        assert (await repository.get_order(command["order_id"])).status == OrderStatus.DELIVERED
        assert audit_types(repository, command["ticket_id"], pending=True) == []

        sql(repository, "DROP TRIGGER fail_refund_audit")
        _, created = await repository.create_refund_once(**command)
        assert created
        assert (await repository.get_order(command["order_id"])).status == OrderStatus.REFUNDED

    async def test_outbox_insert_failure_rolls_back_ticket_approval(self):
        repository = self.repository
        workflow = refund_workflow(repository)
        ticket = await workflow.create_ticket(user_id="user_001", message="订单A12345退款200元")
        assert ticket.state == TicketState.WAITING_APPROVAL
        ticket_id = ticket.ticket_id
        pending_before = audit_types(repository, ticket_id, pending=True)
        sql(repository, """
            CREATE TRIGGER fail_approval_audit BEFORE INSERT ON audit_outbox
            WHEN NEW.event_type = 'ticket.approved'
            BEGIN SELECT RAISE(ABORT, 'injected approval outbox failure'); END
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected approval outbox failure"):
            await workflow.approve(ticket_id, actor="manager_001")

        assert (await repository.get_ticket(ticket_id)).to_dict() == ticket.to_dict()
        assert audit_types(repository, ticket_id, pending=True) == pending_before
        key = ticket.proposed_action["idempotency_key"]
        assert await repository.get_refund_by_idempotency_key(key) is None

    async def test_audit_query_keeps_existing_rows_and_catches_up_only_one_ticket(self):
        repository = self.repository
        workflow = refund_workflow(repository)
        ticket = await workflow.create_ticket(user_id="user_001", message="订单A12345退款200元")
        previously_published = await workflow.get_audit(ticket.ticket_id)
        assert [event["event_type"] for event in previously_published] == [
            "ticket.created", "ticket.planned", "ticket.waiting_approval",
        ]

        other_ticket = await workflow.create_ticket(user_id="demo_user", message="我想申请退款")
        other_pending = audit_types(repository, other_ticket.ticket_id, pending=True)
        assert other_pending

        resolved = await workflow.approve(ticket.ticket_id, actor="manager_001")
        assert resolved.state == TicketState.RESOLVED
        approval_events = ["ticket.approved", "refund.command_accepted", "refund.verified"]
        assert audit_types(repository, ticket.ticket_id, pending=True) == approval_events

        events = await workflow.get_audit(ticket.ticket_id)
        assert events[:len(previously_published)] == previously_published
        assert [event["event_type"] for event in events[len(previously_published):]] == approval_events
        event_ids = [event["event_id"] for event in events]
        assert event_ids == sorted(set(event_ids))
        approval, command, verification = events[-3:]
        assert approval["actor"] == "manager_001"
        assert approval["detail"]["action"] == ticket.proposed_action
        refund_id = resolved.result["refund_id"]
        assert command["detail"]["refund_id"] == refund_id
        assert verification["detail"]["refund_id"] == refund_id
        assert audit_types(repository, ticket.ticket_id, pending=True) == []
        assert audit_types(repository, other_ticket.ticket_id, pending=True) == other_pending
        assert audit_types(repository, other_ticket.ticket_id) == []
        assert await workflow.get_audit(ticket.ticket_id) == events
