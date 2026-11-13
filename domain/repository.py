"""SQLite 售后仓储。

仓储把 SQL、事务和并发控制集中到一个模块。所有公开方法都是异步的，
内部通过 ``asyncio.to_thread`` 执行 sqlite3，避免阻塞 FastAPI 事件循环。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from domain.models import (
    AuditEvent,
    Order,
    OrderStatus,
    RefundRecord,
    RefundStatus,
    RiskLevel,
    Ticket,
    TicketState,
    new_id,
    utc_now,
)


class RepositoryError(RuntimeError):
    """仓储层通用异常。"""


class ConcurrencyError(RepositoryError):
    """工单已被其他请求更新。"""


class SupportRepository:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser().resolve()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    request_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    order_id TEXT,
                    requested_amount REAL,
                    reason TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    result TEXT NOT NULL,
                    error TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL UNIQUE,
                    order_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id),
                    FOREIGN KEY(order_id) REFERENCES orders(order_id)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
                );

                CREATE TABLE IF NOT EXISTS ticket_mentions (
                    user_id TEXT NOT NULL, conv_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id), mentioned_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, conv_id, request_id, ticket_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mentions_ticket ON ticket_mentions(ticket_id, user_id);
                CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_events(ticket_id, event_id);
                """
            )
            self._seed_demo_orders(conn)

    @staticmethod
    def _seed_demo_orders(conn: sqlite3.Connection) -> None:
        now = utc_now()
        rows = [
            ("A12345", "user_001", 299.0, OrderStatus.PAID.value, now),
            ("A10001", "demo_user", 88.0, OrderStatus.DELIVERED.value, now),
            ("A10002", "demo_user", 699.0, OrderStatus.SHIPPED.value, now),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO orders(order_id,user_id,amount,status,created_at) VALUES(?,?,?,?,?)",
            rows,
        )

    async def link_ticket(self, user_id, conv_id, request_id, ticket_id):
        def write():
            with closing(self._connect()) as conn, conn:
                conn.execute("""
                    INSERT OR IGNORE INTO ticket_mentions
                    SELECT user_id, ?, ?, ticket_id, ? FROM tickets
                    WHERE user_id = ? AND ticket_id = ?
                """, (conv_id, request_id, utc_now(), user_id, ticket_id))
        await asyncio.to_thread(write)

    async def status_candidates(self, user_id, conv_id, reference=None, day=None):
        """优先当前会话。显式编号或日期条件始终限制在同一用户名下。"""
        def read():
            with closing(self._connect()) as conn:
                rows = conn.execute("""
                    SELECT t.ticket_id, t.order_id, t.state,
                           MAX(CASE WHEN m.conv_id = ? THEN 1 ELSE 0 END) AS local_match
                    FROM tickets t LEFT JOIN ticket_mentions m
                      ON m.ticket_id = t.ticket_id AND m.user_id = t.user_id
                    WHERE t.user_id = ? AND (? IS NULL OR t.ticket_id = ? OR t.order_id = ?)
                      AND (? IS NULL OR date(t.created_at, '+8 hours') = ?
                           OR date(m.mentioned_at, '+8 hours') = ?)
                    GROUP BY t.ticket_id
                    ORDER BY local_match DESC, t.created_at DESC LIMIT 6
                """, (conv_id, user_id, reference, reference, reference, day, day, day)).fetchall()
                if rows and rows[0]["local_match"] and not reference:
                    rows = [r for r in rows if r["local_match"]]
                return [dict(r) for r in rows]
        return await asyncio.to_thread(read)

    async def get_order(self, order_id: str) -> Optional[Order]:
        return await asyncio.to_thread(self._get_order_sync, order_id)

    def _get_order_sync(self, order_id: str) -> Optional[Order]:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        return self._order_from_row(row) if row else None

    async def create_ticket(self, ticket: Ticket) -> Tuple[Ticket, bool]:
        return await asyncio.to_thread(self._create_ticket_sync, ticket)

    def _create_ticket_sync(self, ticket: Ticket) -> Tuple[Ticket, bool]:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO tickets(
                        ticket_id,user_id,message,request_key,state,action_type,order_id,
                        requested_amount,reason,risk_level,proposed_action,approved_by,
                        approved_at,result,error,version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    self._ticket_values(ticket),
                )
            return ticket, True
        except sqlite3.IntegrityError as ex:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT * FROM tickets WHERE request_key = ?", (ticket.request_key,)
                ).fetchone()
            if row:
                return self._ticket_from_row(row), False
            raise RepositoryError(str(ex)) from ex

    async def get_ticket(self, ticket_id: str) -> Optional[Ticket]:
        return await asyncio.to_thread(self._get_ticket_sync, ticket_id)

    def _get_ticket_sync(self, ticket_id: str) -> Optional[Ticket]:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        return self._ticket_from_row(row) if row else None

    async def save_ticket(self, ticket: Ticket) -> Ticket:
        return await asyncio.to_thread(self._save_ticket_sync, ticket)

    def _save_ticket_sync(self, ticket: Ticket) -> Ticket:
        old_version = ticket.version
        ticket.updated_at = utc_now()
        new_version = old_version + 1
        values = (
            ticket.state.value,
            ticket.order_id,
            ticket.requested_amount,
            ticket.reason,
            ticket.risk_level.value,
            json.dumps(ticket.proposed_action, ensure_ascii=False),
            ticket.approved_by,
            ticket.approved_at,
            json.dumps(ticket.result, ensure_ascii=False),
            ticket.error,
            new_version,
            ticket.updated_at,
            ticket.ticket_id,
            old_version,
        )
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE tickets SET
                    state=?,order_id=?,requested_amount=?,reason=?,risk_level=?,
                    proposed_action=?,approved_by=?,approved_at=?,result=?,error=?,
                    version=?,updated_at=?
                WHERE ticket_id=? AND version=?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"工单 {ticket.ticket_id} 已被其他请求更新")
        ticket.version = new_version
        return ticket

    async def create_refund_once(
        self,
        *,
        ticket_id: str,
        order_id: str,
        user_id: str,
        amount: float,
        idempotency_key: str,
    ) -> Tuple[RefundRecord, bool]:
        return await asyncio.to_thread(
            self._create_refund_once_sync,
            ticket_id,
            order_id,
            user_id,
            amount,
            idempotency_key,
        )

    def _create_refund_once_sync(
        self,
        ticket_id: str,
        order_id: str,
        user_id: str,
        amount: float,
        idempotency_key: str,
    ) -> Tuple[RefundRecord, bool]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                conn.commit()
                return self._refund_from_row(existing), False

            ticket_row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if not ticket_row:
                raise RepositoryError(f"工单不存在: {ticket_id}")
            if ticket_row["state"] != TicketState.EXECUTING.value:
                raise RepositoryError("退款只能在工单进入 EXECUTING 后执行")
            if ticket_row["risk_level"] == RiskLevel.HIGH.value and not ticket_row["approved_by"]:
                raise RepositoryError("高风险退款缺少人工审批")

            order_row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if not order_row:
                raise RepositoryError(f"订单不存在: {order_id}")
            if order_row["user_id"] != user_id:
                raise RepositoryError("订单不属于当前用户")
            if order_row["status"] in (OrderStatus.REFUNDED.value, OrderStatus.CANCELLED.value):
                raise RepositoryError(f"订单状态不允许退款: {order_row['status']}")
            if amount <= 0 or amount > float(order_row["amount"]):
                raise RepositoryError("退款金额必须大于0且不能超过订单金额")

            record = RefundRecord(
                refund_id=new_id("refund"),
                ticket_id=ticket_id,
                order_id=order_id,
                user_id=user_id,
                amount=round(float(amount), 2),
                idempotency_key=idempotency_key,
                status=RefundStatus.SUCCEEDED,
            )
            conn.execute(
                """
                INSERT INTO refunds(
                    refund_id,ticket_id,order_id,user_id,amount,idempotency_key,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record.refund_id,
                    record.ticket_id,
                    record.order_id,
                    record.user_id,
                    record.amount,
                    record.idempotency_key,
                    record.status.value,
                    record.created_at,
                ),
            )
            conn.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?",
                (OrderStatus.REFUNDED.value, order_id),
            )
            conn.commit()
            return record, True
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return self._refund_from_row(existing), False
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def get_refund_by_idempotency_key(self, key: str) -> Optional[RefundRecord]:
        return await asyncio.to_thread(self._get_refund_sync, key)

    def _get_refund_sync(self, key: str) -> Optional[RefundRecord]:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._refund_from_row(row) if row else None

    async def add_audit_event(self, event: AuditEvent) -> AuditEvent:
        event.event_id = await asyncio.to_thread(self._add_audit_event_sync, event)
        return event

    def _add_audit_event_sync(self, event: AuditEvent) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_events(ticket_id,event_type,actor,detail,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    event.ticket_id,
                    event.event_type,
                    event.actor,
                    json.dumps(event.detail, ensure_ascii=False),
                    event.created_at,
                ),
            )
            return int(cursor.lastrowid)

    async def list_audit_events(self, ticket_id: str) -> List[AuditEvent]:
        return await asyncio.to_thread(self._list_audit_events_sync, ticket_id)

    def _list_audit_events_sync(self, ticket_id: str) -> List[AuditEvent]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE ticket_id = ? ORDER BY event_id",
                (ticket_id,),
            ).fetchall()
        return [
            AuditEvent(
                event_id=int(row["event_id"]),
                ticket_id=row["ticket_id"],
                event_type=row["event_type"],
                actor=row["actor"],
                detail=json.loads(row["detail"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> Order:
        return Order(
            order_id=row["order_id"],
            user_id=row["user_id"],
            amount=float(row["amount"]),
            status=OrderStatus(row["status"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _ticket_values(ticket: Ticket) -> tuple:
        return (
            ticket.ticket_id,
            ticket.user_id,
            ticket.message,
            ticket.request_key,
            ticket.state.value,
            ticket.action_type,
            ticket.order_id,
            ticket.requested_amount,
            ticket.reason,
            ticket.risk_level.value,
            json.dumps(ticket.proposed_action, ensure_ascii=False),
            ticket.approved_by,
            ticket.approved_at,
            json.dumps(ticket.result, ensure_ascii=False),
            ticket.error,
            ticket.version,
            ticket.created_at,
            ticket.updated_at,
        )

    @staticmethod
    def _ticket_from_row(row: sqlite3.Row) -> Ticket:
        return Ticket(
            ticket_id=row["ticket_id"],
            user_id=row["user_id"],
            message=row["message"],
            request_key=row["request_key"],
            state=TicketState(row["state"]),
            action_type=row["action_type"],
            order_id=row["order_id"],
            requested_amount=float(row["requested_amount"]) if row["requested_amount"] is not None else None,
            reason=row["reason"],
            risk_level=RiskLevel(row["risk_level"]),
            proposed_action=json.loads(row["proposed_action"] or "{}"),
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            result=json.loads(row["result"] or "{}"),
            error=row["error"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _refund_from_row(row: sqlite3.Row) -> RefundRecord:
        return RefundRecord(
            refund_id=row["refund_id"],
            ticket_id=row["ticket_id"],
            order_id=row["order_id"],
            user_id=row["user_id"],
            amount=float(row["amount"]),
            idempotency_key=row["idempotency_key"],
            status=RefundStatus(row["status"]),
            created_at=row["created_at"],
        )
