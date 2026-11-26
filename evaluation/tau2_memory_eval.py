"""Evaluate recall of an earlier user fact through EchoMind's /chat endpoint."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx

if __package__:
    from .tau2_dialog import EchoMindAgent, run_dialog
else:
    from tau2_dialog import EchoMindAgent, run_dialog
from tau2.data_model.simulation import TerminationReason


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "evaluation" / "tau2_cases" / "memory_context_recall.json"
DEFAULT_REPORT = ROOT / "data" / "eval" / "tau2_memory_baseline.json"
REQUIRED_STAGES = {"memory_read", "intent", "agent_execution"}


def score_dialog(agent, run, *, turn_count, expected_answer):
    reply = agent.replies.get(turn_count - 1, {})
    performance = reply.get("performance", {})
    stages = sorted(REQUIRED_STAGES.intersection(performance.get("stages_ms", {})))
    model_called = any(
        call["stage"].startswith("agent.") and call["status"] == "success"
        for call in performance.get("llm_calls", [])
    )
    answer = reply.get("response", "").strip(" \r\n`\"'「」“”。！!")
    values = [
        ("对话正常结束", TerminationReason.USER_STOP.value, run.termination_reason.value),
        ("成功的 API 回复轮数", turn_count,
         sum(reply.get("status") == "success" for reply in agent.replies.values())),
        ("追问执行了记忆读取、意图识别和 Agent", sorted(REQUIRED_STAGES), stages),
        ("追问成功调用 Agent 模型", True, model_called),
        ("追问答案", expected_answer, answer),
    ]
    return [
        {"name": name, "expected": expected, "actual": actual, "passed": expected == actual}
        for name, expected, actual in values
    ]


def run_trial(client, case):
    user_id = f"tau-memory-{uuid4().hex}"
    results = {}
    # Run the empty-session control first so it cannot learn the fixture from the test.
    for label, messages, answer in (
        ("without_history", [case["probe"]], case["expected_without_history"]),
        ("with_history", [*case["setup_turns"], case["probe"]], case["expected_answer"]),
    ):
        agent = EchoMindAgent(client, user_id)
        run = run_dialog(agent, case_id=f"{case['id']}-{label}",
                         instructions=case["instructions"], messages=messages)
        checks = score_dialog(agent, run, turn_count=len(messages), expected_answer=answer)
        results[label] = {
            "conversation_id": agent.conv_id,
            "checks": checks,
            "replies": agent.replies,
            "simulation": run.model_dump(mode="json"),
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url, timeout=60, trust_env=False) as client:
        client.get("/health").raise_for_status()
        results = run_trial(client, case)

    passed = all(check["passed"] for result in results.values() for check in result["checks"])
    report = {"case_id": case["id"], "score": int(passed), "dialogs": results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for label, result in results.items():
        for check in result["checks"]:
            status = "PASS" if check["passed"] else "FAIL"
            print(f"{status} {label} {check['name']}：预期 {check['expected']!r}，实际 {check['actual']!r}")
    print(f"{case['id']} 得分 {report['score']}，报告保存到 {args.report.resolve()}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
