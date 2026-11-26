"""只读工单追问：从用户自己的场景中定位工单，每次读取最新状态。"""
import re
from datetime import datetime, timedelta, timezone

TICKET = re.compile(r"ticket_[0-9a-f]{12}", re.I)
ORDER = re.compile(r"(?<![A-Za-z0-9])[A-Z][0-9]{4,}(?![A-Za-z0-9])", re.I)
STATUS = re.compile(r"进度|状态|怎么样|到哪|到[账帐]了|退了[吗没]|处理好|审批了|处理完")
STATUS_SUBJECT = re.compile(r"退款|退费|工单|那笔|这笔")
ADDITIONAL_REQUEST = re.compile(r"(?<=[\s，,。；;？！!?])(?:另外|此外|顺便|顺带|同时|还有)\s*[，,：:]?")
LABELS = {"CREATED": "已创建", "COLLECTING_INFO": "等待补充信息", "PLANNING": "正在核验",
          "WAITING_APPROVAL": "等待人工审批", "EXECUTING": "执行中", "VERIFYING": "核验结果中",
          "RESOLVED": "已处理完成", "REJECTED": "已拒绝", "FAILED": "处理失败", "CANCELLED": "已取消"}


def _is_status_question(message, ticket):
    if ticket and message.strip().lower() == ticket.group().lower():
        return True
    return bool(STATUS.search(message) and (ticket or STATUS_SUBJECT.search(message)))


def split_status_request(message: str) -> tuple[str, str | None]:
    """Separate a refund lookup from a clearly introduced additional request."""
    parts = ADDITIONAL_REQUEST.split(message, maxsplit=1)
    if len(parts) == 2:
        first = parts[0].rstrip().rstrip("，,。；;？！!?").strip()
        second = parts[1].strip()
        if first and second:
            first_is_status = _is_status_question(first, TICKET.search(first))
            second_is_status = _is_status_question(second, TICKET.search(second))
            if first_is_status != second_is_status:
                return (first, second) if first_is_status else (second, first)
    return message, None


async def status_answer(repository, user_id, conv_id, message):
    ticket = TICKET.search(message)
    if not _is_status_question(message, ticket):
        return None
    order = ORDER.search(message)
    reference = ticket.group().lower() if ticket else order.group().upper() if order else None
    day = None
    if "昨天" in message or "今天" in message:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        day = (today - timedelta(days=int("昨天" in message))).isoformat()
    rows = await repository.status_candidates(user_id, conv_id, reference, day)
    if not rows:
        return "没有查到对应的退款工单，请核对订单号或工单号。", None
    if len(rows) > 1:
        choices = "、".join(f"{r['order_id'] or '待补订单'}（{r['ticket_id']}）" for r in rows)
        return f"找到多笔退款，请回复具体工单号：{choices}。", None
    row = rows[0]
    return f"工单 {row['ticket_id']} 的最新状态为{LABELS.get(row['state'], row['state'])}。", row["ticket_id"]
