import csv
import unittest
from pathlib import Path
from unittest.mock import patch

from barrybot import DevNetLlmError
from support_agent import SUPPORT_CASES, build_agent_trace, gate_result, run_case, summarize_runs


class FakeLLM:
    model = "devnet-test-model"
    model_source = "devnet-models"
    base_url = "DevNet image LLM proxy"

    def complete(self, messages, max_tokens=96):
        self.messages = messages
        return "I checked the policy and selected the appropriate tool."


class BrokenLLM(FakeLLM):
    def complete(self, messages, max_tokens=96):
        raise DevNetLlmError("model service unavailable")


class SupportAgentTests(unittest.TestCase):
    def test_baseline_fails_release_gate(self):
        runs = [run_case("baseline", case) for case in SUPPORT_CASES]
        self.assertFalse(gate_result(summarize_runs(runs))["passed"])

    def test_candidate_passes_release_gate(self):
        runs = [run_case("candidate", case) for case in SUPPORT_CASES]
        result = gate_result(summarize_runs(runs))
        self.assertTrue(result["passed"])
        self.assertEqual(runs[0]["tool_names"], ["lookup_order", "check_refund_policy", "escalate_case"])

    def test_unknown_variant_is_rejected(self):
        with self.assertRaises(ValueError):
            run_case("future", SUPPORT_CASES[0])

    def test_agent_trace_uses_typed_retriever_output(self):
        llm = FakeLLM()
        run = run_case("candidate", SUPPORT_CASES[0], llm)
        trace = build_agent_trace(run, llm)
        retriever = next(span for span in trace["spans"] if span["type"] == "retriever")
        self.assertIsInstance(retriever["output"], list)
        self.assertEqual(retriever["output"][0]["page_content"],
            "Refunds require authentication and are limited to 30 days after delivery.")
        self.assertEqual(retriever["output"][0]["metadata"]["source"], "refund-policy-v3")

    def test_rejected_refund_is_not_a_completed_action(self):
        run = run_case("baseline", SUPPORT_CASES[2])
        self.assertEqual(run["tools"][-1]["status_code"], 403)
        self.assertEqual(run["metrics"]["action_completion"], 0.0)

    def test_candidate_metrics_follow_failed_tool_results(self):
        tools = run_case("candidate", SUPPORT_CASES[2])["tools"]
        tools[-1]["status_code"] = 503
        tools[-1]["output"] = {"status": "unavailable"}

        with patch("support_agent._tool_path", return_value=tools):
            run = run_case("candidate", SUPPORT_CASES[2])

        self.assertEqual(run["metrics"]["action_completion"], 0.0)
        self.assertEqual(run["metrics"]["tool_errors"], 1.0)
        self.assertFalse(gate_result(run["metrics"])["passed"])

    def test_trace_records_the_actual_summary_request_after_tools(self):
        llm = FakeLLM()
        run = run_case("candidate", SUPPORT_CASES[0], llm)
        trace = build_agent_trace(run, llm)
        summary = next(span for span in trace["spans"] if span["type"] == "llm")
        tools = [span for span in trace["spans"] if span["type"] in ("tool", "retriever")]

        self.assertEqual(summary["input"], llm.messages)
        self.assertGreater(summary["step_number"], max(span["step_number"] for span in tools))
        self.assertEqual(trace["user_metadata"].get("tool_execution"), "simulated")

    def test_fallback_response_does_not_create_a_live_llm_span(self):
        llm = BrokenLLM()
        trace = build_agent_trace(run_case("candidate", SUPPORT_CASES[0], llm), llm)

        self.assertFalse(any(span["type"] == "llm" for span in trace["spans"]))
        self.assertEqual(trace["user_metadata"].get("response_source"), "fallback")
        self.assertTrue(trace["user_metadata"].get("used_fallback"))

    def test_baseline_dataset_outputs_match_executed_scenarios(self):
        path = Path(__file__).resolve().parents[1] / "samples" / "eval_cases.csv"
        with path.open(encoding="utf-8") as handle:
            rows = {row["input"]: row for row in csv.DictReader(handle)}

        for case in SUPPORT_CASES:
            with self.subTest(case=case["case_id"]):
                self.assertEqual(rows[case["input"]]["generated_output"], run_case("baseline", case)["output"])


if __name__ == "__main__":
    unittest.main()
