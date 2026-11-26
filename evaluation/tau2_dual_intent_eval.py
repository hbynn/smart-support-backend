"""Run the refund status and invoice scenario against the isolated EchoMind service."""

import argparse
import json
import sqlite3
from pathlib import Path

import httpx

if __package__:
    from .tau2_dialog import EchoMindAgent, run_dialog
    from .tau2_refund_eval import DB_PATH, ROOT, grade, prepare_tickets, read_results
else:
    from tau2_dialog import EchoMindAgent, run_dialog
    from tau2_refund_eval import DB_PATH, ROOT, grade, prepare_tickets, read_results
from tau2.data_model.simulation import TerminationReason


CASE_PATH = ROOT / "evaluation" / "tau2_cases" / "refund_status_invoice.json"
DEFAULT_REPORT = ROOT / "data" / "eval" / "tau2_dual_intent.json"


def check_invoice_guidance(text, guidance_terms):
    # Look for actionable details in the invoice answer, after any refund-only sentences.
    topic = text.find("发票")
    invoice_answer = text[text.rfind("。", 0, topic) + 1 :] if topic >= 0 else ""
    matching_terms = [term for term in guidance_terms if term in invoice_answer]
    return {
        "name": "回复提供发票补开的办理指引",
        "passed": bool(matching_terms),
        "expected": "发票答复包含至少一项所需信息或办理渠道",
        "actual": {"invoice_answer": invoice_answer.strip(), "matching_terms": matching_terms},
    }


def run_case(client, case):
    health = client.get("/health")
    health.raise_for_status()
    if health.json().get("status") != "ok":
        raise ValueError("评测服务的健康检查状态不是 ok")

    ticket_ids = prepare_tickets(client, case)
    before = read_results(case, ticket_ids)
    expected = [{"state": turn["expected_state"], "refunds": []} for turn in case["turns"]]
    if before != expected:
        raise ValueError(f"隔离测试库的初始状态不符合用例预期，实际为 {before!r}")

    agent = EchoMindAgent(client, case["user_id"])
    run = None
    error = None
    try:
        run = run_dialog(
            agent,
            case_id=case["id"],
            instructions="在同一句话里查询退款进度，同时询问发票补开所需信息。",
            messages=[turn["message"] for turn in case["turns"]],
        )
    except (httpx.HTTPError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    after = read_results(case, ticket_ids)
    reply = agent.replies.get(0, {})
    report = {
        "case_id": case["id"],
        "conversation_id": agent.conv_id,
        "replies": list(agent.replies.values()),
        "database_before": before,
        "database_after": after,
    }
    if run is not None:
        report["simulation"] = run.model_dump(mode="json")
    termination = run.termination_reason.value if run is not None else None
    if termination != TerminationReason.USER_STOP.value or reply.get("status") == "failed":
        report.update(
            status="execution_error",
            score=None,
            error=reply.get("error_code") or error or termination,
        )
        return report

    checks = grade(case, ticket_ids, agent, run, before, after)
    checks.append(check_invoice_guidance(reply.get("response", ""), case["turns"][0]["invoice_guidance_terms"]))
    score = int(all(check["passed"] for check in checks))
    report.update(status="passed" if score else "failed", score=score, checks=checks)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if not DB_PATH.is_file():
        parser.error(f"找不到隔离测试数据库 {DB_PATH}，请先启动评测 Compose 服务。")

    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    try:
        with httpx.Client(base_url=args.base_url, timeout=60, trust_env=False) as client:
            report = run_case(client, case)
    except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
        report = {"case_id": case["id"], "status": "execution_error", "score": None, "error": str(exc)}

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["status"] == "execution_error":
        print(f"执行错误：{report['error']}")
        print(f"报告保存到 {args.report.resolve()}")
        return 2

    checks = report["checks"]
    for failed in (check for check in checks if not check["passed"]):
        print(f"FAIL {failed['name']}，预期 {failed['expected']!r}，实际 {failed['actual']!r}")
    print(f"{case['id']} 通过 {sum(check['passed'] for check in checks)}/{len(checks)} 项，得分 {report['score']}")
    print(f"报告保存到 {args.report.resolve()}")
    return 0 if report["score"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
