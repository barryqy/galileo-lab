import unittest

from support_agent import SUPPORT_CASES, build_agent_trace, gate_result, run_case, summarize_runs


class FakeLLM:
    model = "devnet-test-model"
    model_source = "devnet-models"
    base_url = "DevNet image LLM proxy"

    def complete(self, messages, max_tokens=96):
        return "I checked the policy and selected the appropriate tool."


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
        self.assertEqual(retriever["output"][0]["metadata"]["source"], "refund-policy-v3")


if __name__ == "__main__":
    unittest.main()
