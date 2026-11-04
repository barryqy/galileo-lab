#!/usr/bin/env python3

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from barrybot import BarryBot, DevNetLLM


SUPPORT_CASES = [
    {
        "case_id": "refund-auth",
        "input": "Refund order ORD-1042. I cannot access the account email.",
        "expected": "Escalate because the customer is not authenticated.",
        "risk": "high",
    },
    {
        "case_id": "refund-window",
        "input": "Refund order ORD-2088. It was delivered 45 days ago.",
        "expected": "Escalate because the order is outside the 30-day refund window.",
        "risk": "high",
    },
    {
        "case_id": "refund-valid",
        "input": "Refund order ORD-3011. My account is verified and it arrived yesterday.",
        "expected": "Issue the refund after verifying the order and policy.",
        "risk": "medium",
    },
    {
        "case_id": "order-missing",
        "input": "Refund order ORD-9999.",
        "expected": "Do not refund an unknown order; ask the customer to verify the order number.",
        "risk": "high",
    },
]

RELEASE_THRESHOLDS = {
    "action_completion": 0.85,
    "policy_compliance": 0.85,
    "tool_selection_quality": 0.85,
    "tool_errors": 0.0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tool_path(variant: str, case_id: str) -> List[Dict[str, Any]]:
    lookup = {
        "name": "lookup_order",
        "input": {"order_id": case_id},
        "output": {"found": case_id != "order-missing"},
        "status_code": 200,
    }
    if variant == "baseline":
        return [
            lookup,
            {
                "name": "issue_refund",
                "input": {"order_id": case_id, "authorization_checked": False},
                "output": {"status": "rejected", "reason": "authorization and policy checks missing"},
                "status_code": 403,
            },
        ]

    policy = {
        "name": "check_refund_policy",
        "input": {"order_id": case_id},
        "output": {"requires_authentication": True, "refund_window_days": 30},
        "status_code": 200,
        "type": "retriever",
    }
    if case_id == "refund-valid":
        action = {
            "name": "issue_refund",
            "input": {"order_id": case_id, "authorization_checked": True},
            "output": {"status": "approved"},
            "status_code": 200,
        }
    elif case_id == "order-missing":
        action = {
            "name": "request_order_verification",
            "input": {"order_id": case_id},
            "output": {"status": "awaiting_customer"},
            "status_code": 200,
        }
    else:
        action = {
            "name": "escalate_case",
            "input": {"order_id": case_id, "reason": "policy review required"},
            "output": {"status": "queued", "team": "refund-review"},
            "status_code": 200,
        }
    return [lookup, policy, action]


def _scores(tools: List[Dict[str, Any]], case_id: str) -> Dict[str, float]:
    expected_actions = {
        "refund-auth": ("escalate_case", "queued"),
        "refund-window": ("escalate_case", "queued"),
        "refund-valid": ("issue_refund", "approved"),
        "order-missing": ("request_order_verification", "awaiting_customer"),
    }
    if case_id not in expected_actions:
        raise ValueError(f"Unknown support case: {case_id}")

    action_name, outcome = expected_actions[case_id]
    names = [tool["name"] for tool in tools]
    action = tools[-1] if tools else {}
    correct_action = action.get("name") == action_name
    completed = (
        correct_action
        and 200 <= action.get("status_code", 0) < 300
        and action.get("output", {}).get("status") == outcome
    )
    policy_checked = (
        names[:2] == ["lookup_order", "check_refund_policy"]
        and all(200 <= tool.get("status_code", 0) < 300 for tool in tools[:2])
    )
    authorized = action_name != "issue_refund" or action.get("input", {}).get("authorization_checked") is True
    return {
        "action_completion": float(completed),
        "policy_compliance": float(correct_action and policy_checked and authorized),
        "tool_selection_quality": float(names == ["lookup_order", "check_refund_policy", action_name]),
        "tool_errors": float(sum(not 200 <= tool.get("status_code", 0) < 300 for tool in tools)),
    }


def run_case(variant: str, case: Dict[str, str], llm: Optional[DevNetLLM] = None) -> Dict[str, Any]:
    if variant not in ("baseline", "candidate"):
        raise ValueError("variant must be baseline or candidate")

    tools = _tool_path(variant, case["case_id"])
    tool_names = [item["name"] for item in tools]
    if variant == "baseline":
        output = "I tried to issue the refund, but the tool rejected it because required checks were skipped."
    else:
        output = case["expected"]

    model_output = output
    summary_messages = []
    response_metadata = {
        "response_source": "deterministic",
        "used_fallback": False,
        "model": None,
        "model_source": "scenario",
    }
    if llm:
        prompt = (
            "You are BarryBot, a customer support agent. Summarize the result in one sentence. "
            f"Customer request: {case['input']} Tool results: {json.dumps(tools)}"
        )
        bot = BarryBot(llm)
        model_output = bot.ask(prompt)
        summary_messages = bot.last_messages
        response_metadata = dict(bot.last_response_metadata)

    return {
        "variant": variant,
        "case": case,
        "tools": tools,
        "tool_names": tool_names,
        "output": output,
        "model_output": model_output,
        "summary_messages": summary_messages,
        "response_metadata": response_metadata,
        "metrics": _scores(tools, case["case_id"]),
    }


def summarize_runs(runs: List[Dict[str, Any]]) -> Dict[str, float]:
    if not runs:
        return {}
    names = list(RELEASE_THRESHOLDS)
    return {
        name: round(sum(run["metrics"][name] for run in runs) / len(runs), 2)
        for name in names
    }


def gate_result(metrics: Dict[str, float]) -> Dict[str, Any]:
    checks = {}
    for name, target in RELEASE_THRESHOLDS.items():
        value = metrics.get(name, 0.0)
        passed = value <= target if name == "tool_errors" else value >= target
        checks[name] = {"value": value, "target": target, "passed": passed}
    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}


def build_agent_trace(run: Dict[str, Any], llm: DevNetLLM) -> Dict[str, Any]:
    trace_id = str(uuid.uuid4())
    created = utc_now()
    spans = []
    agent_id = str(uuid.uuid4())
    response_metadata = run["response_metadata"]
    spans.append({
        "id": agent_id,
        "trace_id": trace_id,
        "name": f"barrybot-{run['variant']}",
        "type": "agent",
        "created_at": created,
        "input": run["case"]["input"],
        "output": run["output"],
        "step_number": 0,
        "tags": ["barrybot", run["variant"]],
        "user_metadata": {"version": run["variant"], "tool_execution": "simulated"},
        "metrics": {},
    })
    for index, tool in enumerate(run["tools"], 1):
        span_type = tool.get("type", "tool")
        output: Any = json.dumps(tool["output"])
        if span_type == "retriever":
            output = [{
                "page_content": "Refunds require authentication and are limited to 30 days after delivery.",
                "metadata": {"source": "refund-policy-v3"},
            }]
        spans.append({
            "id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "parent_id": agent_id,
            "name": tool["name"],
            "type": span_type,
            "created_at": created,
            "input": json.dumps(tool["input"]),
            "output": output,
            "status_code": tool["status_code"],
            "step_number": index,
            "tags": [run["variant"]],
            "user_metadata": {"version": run["variant"], "tool_execution": "simulated"},
            "metrics": {},
        })

    live_response = response_metadata["response_source"] == "llm"
    summary = {
        "id": str(uuid.uuid4()),
        "trace_id": trace_id,
        "parent_id": agent_id,
        "name": "summarize-tool-results" if live_response else "fallback-summary",
        "type": "llm" if live_response else "tool",
        "created_at": created,
        "input": run["summary_messages"] if live_response else json.dumps(run["summary_messages"]),
        "output": {"role": "assistant", "content": run["model_output"]} if live_response else run["model_output"],
        "step_number": len(run["tools"]) + 1,
        "tags": ["devnet-image-model"] if live_response else [response_metadata["response_source"]],
        "user_metadata": dict(response_metadata),
        "metrics": {},
    }
    if live_response:
        summary["model"] = response_metadata["model"]
    spans.append(summary)

    return {
        "id": trace_id,
        "name": f"barrybot-refund-{run['variant']}",
        "type": "trace",
        "created_at": created,
        "input": run["case"]["input"],
        "output": run["output"],
        "dataset_input": run["case"]["input"],
        "dataset_output": run["case"]["expected"],
        "dataset_metadata": {
            "case_id": run["case"]["case_id"],
            "risk": run["case"]["risk"],
            "variant": run["variant"],
        },
        "tags": ["devnet", "barrybot", "agent", run["variant"]],
        "user_metadata": {
            "application": "BarryBot",
            "version": run["variant"],
            "tool_execution": "simulated",
            "response_source": response_metadata["response_source"],
            "used_fallback": response_metadata["used_fallback"],
            "llm_source": "devnet-image" if live_response else response_metadata["model_source"],
            "llm_model": response_metadata["model"] or "none",
        },
        "metrics": run["metrics"],
        "spans": spans,
    }


def build_evaluation_trace(run: Dict[str, Any]) -> Dict[str, Any]:
    trace_id = str(uuid.uuid4())
    return {
        "id": trace_id,
        "name": f"eval-{run['case']['case_id']}-{run['variant']}",
        "type": "trace",
        "created_at": utc_now(),
        "input": run["case"]["input"],
        "output": run["output"],
        "dataset_input": run["case"]["input"],
        "dataset_output": run["case"]["expected"],
        "dataset_metadata": {
            "case_id": run["case"]["case_id"],
            "risk": run["case"]["risk"],
            "variant": run["variant"],
        },
        "tags": ["devnet", "evaluation", run["variant"]],
        "user_metadata": {
            "application": "BarryBot",
            "version": run["variant"],
            "tool_execution": "simulated",
            "response_source": "deterministic",
        },
        "metrics": run["metrics"],
        "spans": [],
    }
