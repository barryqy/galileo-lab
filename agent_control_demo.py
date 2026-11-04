#!/usr/bin/env python3

import asyncio
from typing import Any, Callable, Optional

from agent_control import AgentControlClient
from agent_control.evaluation import check_evaluation_with_local
from agent_control_engine import discover_evaluators


CONTROL_NAME = "barrybot-credential-exfiltration"
REFUSAL = "I cannot help with requests that expose private credentials."


def credential_control() -> dict[str, Any]:
    return {
        "id": 1,
        "name": CONTROL_NAME,
        "control": {
            "enabled": True,
            "execution": "sdk",
            "scope": {"stages": ["pre"], "step_types": ["llm"]},
            "condition": {
                "selector": {"path": "input.message"},
                "evaluator": {
                    "name": "regex",
                    "config": {
                        "pattern": r"(?i)\b(passwords?|credentials?|private support tokens?|support tokens?|secrets?|api[ _-]?keys?|access[ _-]?tokens?)\b",
                    },
                },
            },
            "action": {"decision": "deny"},
        },
    }


async def _evaluate(prompt: str):
    discover_evaluators()
    # Every control runs in the SDK; this client is never used for a network call.
    async with AgentControlClient(base_url="http://127.0.0.1:9") as client:
        return await check_evaluation_with_local(
            client,
            agent_name="barrybot-devnet",
            step={"type": "llm", "name": "support-response", "input": {"message": prompt}},
            stage="pre",
            controls=[credential_control()],
        )


def _refund_policy(prompt: str) -> str:
    return "Refund requests follow the order's refund policy and approval limits."


def run_guardrail(prompt: str, protected_call: Optional[Callable[[str], Any]] = None) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty prompt is required")

    try:
        result = asyncio.run(_evaluate(prompt))
    except Exception as exc:
        raise RuntimeError("Agent Control evaluation failed; the protected function was not run") from exc

    if result.errors:
        raise RuntimeError("Agent Control evaluation failed; the protected function was not run")

    evaluated = result.non_matches if result.is_safe else result.matches
    if not evaluated or not any(item.control_name == CONTROL_NAME and item.action == "deny" for item in evaluated):
        raise RuntimeError("Agent Control did not evaluate the required control; the protected function was not run")

    executed = bool(result.is_safe)
    response = REFUSAL
    if executed:
        response = (protected_call or _refund_policy)(prompt)

    return {
        "engine": "Agent Control SDK",
        "execution": "sdk",
        "control_name": CONTROL_NAME,
        "decision": "ALLOW" if executed else "DENY",
        "executed": executed,
        "protected_calls": 1 if executed else 0,
        "response": response,
        "confidence": result.confidence,
    }
