"""Run the fixed refund scenario with τ-Bench v2.0.0 and score EchoMind's SQLite state."""

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

if __package__:
    from .tau2_dialog import EchoMindAgent, run_dialog
else:
    from tau2_dialog import EchoMindAgent, run_dialog
from tau2.data_model.simulation import TerminationReason


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evaluation" / "tau2_cases" / "refund_order_switch.json"
DB_PATH = ROOT / "data" / "support" / "eval.db"
DEFAULT_REPORT = ROOT / "data" / "eval" / "tau2_refund_baseline.json"
TICKET_ID = re.compile(r"\bticket_[0-9a-f]{12}\b", re.IGNORECASE)


def prepare_tickets(client, case):
    ticket_ids = []
    for turn in case["turns"]:
        order_id, amount = turn["order_id"], turn["amount"]
        response = client.post(
            "/tickets",
            json={
                "user_id": case["user_id"],
                "order_id": order_id,
                "amount": amount,
                "message": f"申请订单 {order_id} 退款 {amount} 元",
            },
        )
        response.raise_for_status()
        ticket_ids.append(response.json()["ticket_id"])
    return ticket_ids


def read_results(case, ticket_ids):
    """Read a consistent snapshot of the two demo orders without writing to SQLite."""
    with closing(sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        results = []
        for turn, ticket_id in zip(case["turns"], ticket_ids, strict=True):
            order = (case["user_id"], turn["order_id"])
            ticket = db.execute(
                "SELECT state FROM tickets WHERE user_id = ? AND order_id = ? AND ticket_id = ?",
                (*order, ticket_id),
            ).fetchone()
            refunds = db.execute(
                "SELECT ticket_id, amount, status FROM refunds "
                "WHERE user_id = ? AND order_id = ? ORDER BY ticket_id",
                order,
            ).fetchall()
            results.append({
                "state": ticket["state"] if ticket else None,
                "refunds": [dict(row) for row in refunds],
            })
        return results


def grade(case, ticket_ids, agent, run, before, after):
    checks = []

    def check(name, expected, actual):
        checks.append({
            "name": name, "passed": expected == actual,
            "expected": expected, "actual": actual,
        })

    check("对话正常结束", TerminationReason.USER_STOP.value, run.termination_reason.value)
    check("收到的回复轮数", len(case["turns"]), len(agent.replies))
    for index, (turn, ticket_id) in enumerate(zip(case["turns"], ticket_ids, strict=True)):
        label = f"第 {index + 1} 轮（{turn['order_id']}）"
        reply = agent.replies.get(index, {})
        text = reply.get("response", "")
        expected_status = turn["expected_reply_status"]
        expected_refunds = [
            {"ticket_id": ticket_id, **refund} for refund in turn["expected_refunds"]
        ]
        check(f"{label} 接口状态", "success", reply.get("status"))
        check(f"{label} 回复里的工单", [ticket_id], sorted(set(TICKET_ID.findall(text.lower()))))
        check(f"{label} 回复里的状态", expected_status, expected_status if expected_status in text else text)
        check(f"{label} 数据库工单终态", turn["expected_state"], after[index]["state"])
        check(f"{label} 数据库退款记录", expected_refunds, after[index]["refunds"])
        check(f"{label} 查询前后的状态和退款记录一致", before[index], after[index])
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if not DB_PATH.is_file():
        parser.error(f"找不到隔离测试数据库 {DB_PATH}，请先启动评测 Compose 服务。")

    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url, timeout=60, trust_env=False) as client:
        client.get("/health").raise_for_status()
        ticket_ids = prepare_tickets(client, case)
        before = read_results(case, ticket_ids)

        agent = EchoMindAgent(client, case["user_id"])
        run = run_dialog(
            agent,
            case_id=case["id"],
            instructions="查询退款进度，再纠正订单号。",
            messages=[turn["message"] for turn in case["turns"]],
        )
        after = read_results(case, ticket_ids)

    checks = grade(case, ticket_ids, agent, run, before, after)
    passed = sum(check["passed"] for check in checks)
    report = {
        "case_id": case["id"],
        "conversation_id": agent.conv_id,
        "score": int(passed == len(checks)),
        "checks": checks,
        "replies": list(agent.replies.values()),
        "simulation": run.model_dump(mode="json"),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for failed in (check for check in checks if not check["passed"]):
        print(f"FAIL {failed['name']}，预期 {failed['expected']!r}，实际 {failed['actual']!r}")
    print(f"{case['id']} 通过 {passed}/{len(checks)} 项，得分 {report['score']}")
    print(f"报告保存到 {args.report.resolve()}")
    return 0 if report["score"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
