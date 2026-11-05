import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from barrybot import DevNetLlmError
from galileo_client import GalileoApiError, api_payload

try:
    import observability_tests as workflow
except ImportError:
    workflow = None


class MemoryClient:
    base_url = "https://api.example.test"

    def __init__(self):
        self.traces = {}
        self.payloads = []
        self.reads = []
        self.pending = {}

    def post(self, path, *, json):
        payload = api_payload(json)
        self.payloads.append(payload)
        for trace in payload["traces"]:
            self.traces[trace["id"]] = copy.deepcopy(trace)
        return {"trace_ids": [trace["id"] for trace in payload["traces"]],
            "traces_count": len(payload["traces"])}

    def get(self, path):
        trace_id = path.rsplit("/", 1)[1]
        self.reads.append(trace_id)
        if self.pending.get(trace_id, 0):
            self.pending[trace_id] -= 1
            raise GalileoApiError("GET", path, 404, "not indexed")
        return copy.deepcopy(self.traces[trace_id])


class TestLLM:
    model = "test-model"
    last_completion_metadata = {}

    def __init__(self, usage=True, fail=False):
        self.usage = usage
        self.fail = fail
        self.calls = []

    def complete(self, messages, *, max_tokens):
        self.calls.append((messages, max_tokens))
        if self.fail:
            raise DevNetLlmError("unavailable")
        self.last_completion_metadata = {
            "created_at": "2026-11-04T02:00:00Z",
            "duration_ns": 120_000_000,
            "total_duration_ns": 150_000_000,
            "request_count": 1,
            "model": self.model,
        }
        if self.usage:
            self.last_completion_metadata.update({
                "num_input_tokens": 40, "num_output_tokens": 12, "num_total_tokens": 52,
            })
        return "Use the documented support process."


class AgentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(workflow, "get_agent_name", None)), "Persistent agent identity is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "workshop"

    def test_identity_is_private_readable_and_reused_after_an_independent_process(self):
        name = workflow.get_agent_name(self.state_dir)
        self.assertRegex(name, r"^(?:[A-Z][a-z]+){4}-[a-f0-9]{6}$")
        self.assertLessEqual(len(name), 64)
        path = self.state_dir / "agent.json"
        self.assertEqual(json.loads(path.read_text())["agent_name"], name)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        result = subprocess.run([sys.executable, "-B", "-c",
            "from pathlib import Path; from observability_tests import get_agent_name; "
            "import sys; print(get_agent_name(Path(sys.argv[1])))", str(self.state_dir)],
            text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), name)
        self.assertEqual(workflow.get_agent_name(self.state_dir), name)

    def test_concurrent_processes_publish_one_complete_identity(self):
        command = [sys.executable, "-B", "-c",
            "from pathlib import Path; from observability_tests import get_agent_name; "
            "import sys; print(get_agent_name(Path(sys.argv[1])))", str(self.state_dir)]
        processes = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE) for _ in range(8)]
        names = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            names.append(stdout.strip())
        self.assertEqual(len(set(names)), 1)
        self.assertEqual(json.loads((self.state_dir / "agent.json").read_text())["agent_name"], names[0])

    def test_invalid_identity_is_rejected_without_replacing_it(self):
        self.state_dir.mkdir()
        path = self.state_dir / "agent.json"
        for content in ("{", "[]", '{}', '{"agent_name": "person@example.test"}',
                '{"agent_name": "not-a-four-word-name"}', '{"agent_name": null}'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "identity"):
                workflow.get_agent_name(self.state_dir)
            self.assertEqual(path.read_text(), content)

    def test_symlink_identity_is_rejected_without_changing_its_target(self):
        self.state_dir.mkdir()
        target = Path(self.tmp.name) / "elsewhere.json"
        content = '{"agent_name": "CalmCedarOtterTrail-a1b2c3"}'
        target.write_text(content)
        (self.state_dir / "agent.json").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "identity"):
            workflow.get_agent_name(self.state_dir)
        self.assertTrue((self.state_dir / "agent.json").is_symlink())
        self.assertEqual(target.read_text(), content)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(workflow, "The additive observability workflow is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.client = MemoryClient()
        self.ids = {"project_id": "project", "log_stream_id": "stream"}
        self.pause = patch.object(workflow.time, "sleep")
        self.pause.start()
        self.addCleanup(self.pause.stop)

    def run_suite(self, suite="baseline", llm=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return workflow.run_suite(self.client, self.ids, llm or TestLLM(), suite,
                state_dir=self.state_dir, console_url="https://console.example.test/tenant")

    def test_baseline_efficient_and_privacy_share_one_agent_with_exact_batch_markers(self):
        reports = [self.run_suite(suite) for suite in ("baseline", "efficient", "privacy")]
        self.assertTrue(reports[0].get("agent_name"), "Suite runs need a persistent agent name")
        agent_name = reports[0]["agent_name"]
        for report, payload in zip(reports, self.client.payloads):
            self.assertEqual(report["agent_name"], agent_name)
            manifest = json.loads((self.state_dir / "runs" / f"{report['run_id']}.json").read_text())
            self.assertEqual(manifest["agent_name"], agent_name)
            for trace in payload["traces"]:
                self.assertEqual(trace["user_metadata"]["agent_name"], agent_name)
                self.assertIn(agent_name, trace["tags"])
                self.assertTrue(trace["name"].startswith(agent_name + "-"))
                self.assertIn(report["run_id"], trace["name"])
                self.assertIn(trace["user_metadata"]["case"], trace["name"])
                self.assertTrue(all(span["user_metadata"]["agent_name"] == agent_name
                    for span in trace["spans"]))

    def test_new_run_prints_agent_before_test_progress(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = workflow.run_suite(self.client, self.ids, TestLLM(), "baseline",
                state_dir=self.state_dir, console_url="https://console.example.test/tenant")
        self.assertIn("Your agent: ", output.getvalue())
        self.assertTrue(output.getvalue().startswith(f"Your agent: {report['agent_name']}\n"))

    def test_alias_mismatches_in_readback_are_rejected(self):
        report = self.run_suite()
        trace = self.client.traces[report["trace_ids"][0]]
        self.assertIn("agent_name", trace["user_metadata"], "Alias metadata is missing")
        original = copy.deepcopy(trace)
        mutations = [
            lambda row: row["user_metadata"].pop("agent_name"),
            lambda row: row["user_metadata"].update(agent_name="OtherCedarOtterTrail-a1b2c3"),
            lambda row: row.update(tags=[tag for tag in row["tags"] if tag != report["agent_name"]]),
            lambda row: row.update(name="other-" + row["name"]),
            lambda row: row["spans"][0]["user_metadata"].pop("agent_name"),
            lambda row: row["spans"][1]["user_metadata"].update(agent_name="OtherCedarOtterTrail-a1b2c3"),
        ]
        for index, mutate in enumerate(mutations):
            altered = copy.deepcopy(original)
            mutate(altered)
            self.client.traces[trace["id"]] = altered
            with self.subTest(mutation=index), self.assertRaisesRegex(ValueError, "agent"):
                workflow.report_findings(self.client, state_dir=self.state_dir)

    def test_findings_and_ui_identify_agent_batch_and_saved_direct_link(self):
        report = self.run_suite()
        first = self.client.traces[report["trace_ids"][0]]
        first["session_id"] = "stored-session"
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIn("agent_name", report, "Findings need the saved alias")
        for printer in (workflow.print_findings, workflow.print_ui):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                printer(report)
            self.assertIn(f"Your agent: {report['agent_name']}", output.getvalue())
            self.assertIn(report["run_id"], output.getvalue())
            self.assertIn("/sessions/stored-session?", output.getvalue())
            self.assertIn(f"traceId={first['id']}", output.getvalue())

    def test_cross_agent_or_aliased_and_legacy_runs_are_not_compared(self):
        self.run_suite()
        efficient = self.run_suite("efficient")
        path = self.state_dir / "runs" / f"{efficient['run_id']}.json"
        manifest = json.loads(path.read_text())
        for alias in ("OtherCedarOtterTrail-a1b2c3", None):
            if alias is None:
                manifest.pop("agent_name", None)
            else:
                manifest["agent_name"] = alias
            path.write_text(json.dumps(manifest))
            self.client.reads.clear()
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "agent"):
                workflow.compare_runs(self.client, state_dir=self.state_dir)
            self.assertEqual(self.client.reads, [])

    def test_legacy_manifests_keep_readback_links_and_pair_comparison_without_an_alias(self):
        baseline = self.run_suite()
        efficient = self.run_suite("efficient")
        for report in (baseline, efficient):
            path = self.state_dir / "runs" / f"{report['run_id']}.json"
            manifest = json.loads(path.read_text())
            manifest.pop("agent_name", None)
            path.write_text(json.dumps(manifest))
        for trace in self.client.traces.values():
            trace["user_metadata"].pop("agent_name", None)
            trace["name"] = f"lab-{trace['user_metadata']['run_id']}-{trace['user_metadata']['case']}"
            trace["session_id"] = "legacy-session"
            for span in trace["spans"]:
                span["user_metadata"].pop("agent_name", None)
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir, run_id=baseline["run_id"])
        self.assertNotIn("agent_name", fresh)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(fresh)
            workflow.print_ui(fresh)
            workflow.compare_runs(self.client, state_dir=self.state_dir)
        self.assertNotIn("Your agent:", output.getvalue())
        self.assertIn("/sessions/legacy-session?", output.getvalue())
        self.assertIn(baseline["run_id"], output.getvalue())
        self.assertIn(efficient["run_id"], output.getvalue())

    def test_saved_run_identifies_exact_uploaded_traces_and_fresh_api_values(self):
        report = self.run_suite()
        manifest = json.loads((self.state_dir / "runs" / f"{report['run_id']}.json").read_text())
        self.assertEqual(len(manifest["trace_ids"]), 3)
        self.assertEqual(set(self.client.reads), set(manifest["trace_ids"]))
        self.assertTrue(report["api_verified"])
        self.assertEqual(report["totals"]["input_tokens"], 120)
        self.assertEqual(report["totals"]["output_tokens"], 36)
        self.assertEqual(report["totals"]["model_ms"], 360)
        self.assertIsNone(report["totals"]["cost_usd"])
        self.assertTrue(self.client.payloads[0]["reliable"])
        self.assertTrue(self.client.payloads[0]["include_trace_ids"])
        trace_id = manifest["trace_ids"][0]
        self.client.traces[trace_id]["spans"][1]["metrics"]["num_input_tokens"] = 99
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertEqual(fresh["totals"]["input_tokens"], 179)

    def test_missing_usage_remains_unavailable_without_zero_or_estimate(self):
        report = self.run_suite(llm=TestLLM(usage=False))
        self.assertIsNone(report["totals"]["input_tokens"])
        self.assertIsNone(report["totals"]["total_tokens"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertIn("unavailable", output.getvalue().lower())
        self.assertIn("tariff", output.getvalue().lower())

    def test_native_model_cost_sums_successful_spans_without_root_rollup_duplicates(self):
        self.run_suite()
        for trace in self.client.traces.values():
            trace["metrics"]["cost"] = 99
            trace["metric_info"] = {"cost": {"status_type": "roll_up", "value": 99}}
            model = next(span for span in trace["spans"] if span["type"] == "llm")
            model["metrics"]["cost"] = 0.0011500000255
            model["metric_info"] = {"cost": {"status_type": "success", "value": 0.0011500000255}}
            trace["spans"].append(copy.deepcopy(model))
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIsNotNone(report["totals"]["cost_usd"])
        self.assertAlmostEqual(report["totals"]["cost_usd"], 3 * 0.0011500000255)
        self.assertAlmostEqual(report["cases"][0]["cost_usd"], 0.0011500000255)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertIn("$0.001150", output.getvalue())
        self.assertIn("$0.003450", output.getvalue())
        self.assertIn("Galileo-reported model cost; not DevNet billing", output.getvalue())

    def test_unsuccessful_native_cost_with_numeric_value_stays_unavailable(self):
        self.run_suite()
        for status in ("pending", "computing", "error", "not_applicable"):
            for trace in self.client.traces.values():
                model = next(span for span in trace["spans"] if span["type"] == "llm")
                model["metrics"]["cost"] = 0.00115
                model["metric_info"] = {"cost": {"status_type": status, "value": 0.00115}}
            with self.subTest(status=status):
                report = workflow.report_findings(self.client, state_dir=self.state_dir)
                self.assertIsNone(report["totals"]["cost_usd"])
                self.assertTrue(all(row["cost_usd"] is None for row in report["cases"]))

    def test_partial_native_cost_does_not_present_partial_sum_as_run_total(self):
        self.run_suite()
        first_trace = next(iter(self.client.traces.values()))
        model = next(span for span in first_trace["spans"] if span["type"] == "llm")
        model["metric_info"] = {"cost": {"status_type": "success", "value": 0.00115}}
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIsNotNone(report["cases"][0]["cost_usd"])
        self.assertAlmostEqual(report["cases"][0]["cost_usd"], 0.00115)
        self.assertIsNone(report["totals"]["cost_usd"])
        self.assertIn("some", report["cost_reason"])

    def test_output_limit_warning_uses_stored_finish_reason_and_missing_stays_unknown(self):
        class CappedLLM(TestLLM):
            def complete(self, messages, *, max_tokens):
                answer = super().complete(messages, max_tokens=max_tokens)
                self.last_completion_metadata["finish_reason"] = "length"
                return answer

        report = self.run_suite(llm=CappedLLM())
        self.assertEqual(report["cases"][0].get("finish_reason"), "length")
        self.assertEqual(report["cases"][0]["status"], "completed")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertIn("response reached output limit; inspect completeness", output.getvalue())
        for trace in self.client.traces.values():
            for span in trace["spans"]:
                span.pop("finish_reason", None)
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIsNone(fresh["cases"][0]["finish_reason"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(fresh)
        self.assertNotIn("response reached output limit", output.getvalue())

    def test_partial_indexing_retries_then_marks_pending_without_cached_values(self):
        report = self.run_suite()
        first, second, _ = report["trace_ids"]
        self.client.pending = {first: 1, second: 99}
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertFalse(fresh["api_verified"])
        self.assertEqual(fresh["cases"][0]["status"], "completed")
        self.assertEqual(fresh["cases"][1]["status"], "pending")
        self.assertIsNone(fresh["cases"][1]["input_tokens"])
        self.assertIsNone(fresh["totals"]["input_tokens"])
        self.assertLessEqual(self.client.reads.count(second), 6)

    def test_destination_and_run_metadata_mismatches_are_rejected(self):
        report = self.run_suite()
        self.client.base_url = "https://other.example.test"
        with self.assertRaisesRegex(ValueError, "destination"):
            workflow.report_findings(self.client, state_dir=self.state_dir)
        self.client.base_url = MemoryClient.base_url
        self.client.traces[report["trace_ids"][0]]["user_metadata"]["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run"):
            workflow.report_findings(self.client, state_dir=self.state_dir)

    def test_extended_trace_project_and_stream_identifiers_must_match(self):
        report = self.run_suite()
        trace = self.client.traces[report["trace_ids"][0]]
        trace.pop("log_stream_id")
        trace.update(project_id="project", run_id="stream")
        self.assertTrue(workflow.report_findings(self.client, state_dir=self.state_dir)["api_verified"])
        for key in ("project_id", "run_id"):
            original = trace[key]
            trace[key] = "another-workspace"
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "destination"):
                workflow.report_findings(self.client, state_dir=self.state_dir)
            trace[key] = original

    def test_llm_timestamp_uses_the_successful_provider_attempt_start(self):
        self.run_suite()
        for trace in self.client.payloads[0]["traces"]:
            llm_span = next(span for span in trace["spans"] if span["type"] == "llm")
            self.assertEqual(llm_span["created_at"], "2026-11-04T02:00:00Z")

    def test_nonfinite_api_metrics_are_unavailable(self):
        self.run_suite()
        for trace in self.client.traces.values():
            llm_span = next(span for span in trace["spans"] if span["type"] == "llm")
            llm_span["metrics"]["duration_ns"] = float("inf")
            llm_span["metrics"]["num_input_tokens"] = float("nan")
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIsNone(report["totals"]["model_ms"])
        self.assertIsNone(report["totals"]["input_tokens"])

    def test_upload_count_or_returned_ids_mismatch_does_not_mark_run_accepted(self):
        original_post = self.client.post
        for key, value in (("traces_count", 1), ("trace_ids", ["other-trace"]),
                ("trace_ids", [{"id": "malformed"}, "other", "third"])):
            def incorrect_reply(path, *, json):
                result = original_post(path, json=json)
                result[key] = value
                return result

            self.client.post = incorrect_reply
            with self.subTest(field=key), self.assertRaisesRegex(RuntimeError, "upload"):
                self.run_suite()
            latest = json.loads((self.state_dir / "latest_run.json").read_text())["run_id"]
            manifest = json.loads((self.state_dir / "runs" / f"{latest}.json").read_text())
            self.assertNotEqual(manifest["upload_status"], "accepted")

    def test_privacy_fixtures_are_fictional_and_summaries_exclude_the_values(self):
        report = self.run_suite("privacy")
        self.assertEqual(len(report["cases"]), 2)
        payload = self.client.payloads[0]
        prompts = " ".join(trace["input"] for trace in payload["traces"])
        self.assertIn("example.test", prompts)
        self.assertIn("555-01", prompts)
        self.assertEqual(report["cases"][0]["pii_input"], ["email", "phone"])
        self.assertEqual(report["cases"][1]["pii_input"], [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertNotIn("learner@example.test", output.getvalue())
        self.assertIn("local pattern", output.getvalue().lower())
        self.assertIn("not Galileo", output.getvalue())

    def test_model_errors_are_logged_without_successful_llm_spans(self):
        report = self.run_suite(llm=TestLLM(fail=True))
        self.assertEqual([row["status"] for row in report["cases"]], ["model_error"] * 3)
        self.assertEqual(report["totals"]["completed_cases"], 0)
        self.assertIsNone(report["totals"]["input_tokens"])
        for trace in self.client.payloads[0]["traces"]:
            self.assertFalse(any(span["type"] == "llm" for span in trace["spans"]))

    def test_missing_stored_pattern_findings_are_not_reported_as_no_matches(self):
        report = self.run_suite("privacy")
        for trace in self.client.traces.values():
            trace["user_metadata"].pop("pii_input")
            trace["user_metadata"].pop("pii_output")
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertIsNone(report["cases"][0]["pii_input"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertIn("PII unavailable", output.getvalue())

    def test_retry_usage_is_not_presented_as_a_complete_bill(self):
        class RetriedLLM(TestLLM):
            def complete(self, messages, *, max_tokens):
                answer = super().complete(messages, max_tokens=max_tokens)
                self.last_completion_metadata["request_count"] = 2
                return answer

        report = self.run_suite(llm=RetriedLLM())
        self.assertEqual(report["totals"].get("request_count"), 6)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_findings(report)
        self.assertIn("successful responses", output.getvalue())
        self.assertIn("failed attempts", output.getvalue())

    def test_incomplete_span_readback_waits_for_the_exact_trace_children(self):
        report = self.run_suite()
        original_get = self.client.get
        seen = set()

        def partially_indexed(path):
            trace = original_get(path)
            if trace["id"] not in seen:
                seen.add(trace["id"])
                trace.pop("spans")
            return trace

        self.client.get = partially_indexed
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertTrue(fresh["api_verified"])
        self.assertEqual(fresh["totals"]["input_tokens"], 120)
        self.assertEqual(seen, set(report["trace_ids"]))

    def test_partially_indexed_children_do_not_claim_a_complete_api_readback(self):
        report = self.run_suite()
        for trace in self.client.traces.values():
            trace["spans"] = trace["spans"][:1]
        fresh = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertFalse(fresh["api_verified"])
        self.assertTrue(all(row["status"] == "pending" for row in fresh["cases"]))
        self.assertIsNone(fresh["totals"]["model_ms"])

    def test_suite_comparison_reads_saved_exact_ids_and_identifies_controlled_delay(self):
        baseline_llm, efficient_llm = TestLLM(), TestLLM()
        baseline = self.run_suite(llm=baseline_llm)
        efficient = self.run_suite("efficient", efficient_llm)
        self.assertEqual([calls[0][-1]["content"] for calls in baseline_llm.calls],
            [calls[0][-1]["content"] for calls in efficient_llm.calls])
        self.assertEqual(baseline_llm.calls[0][1], 180)
        self.assertEqual(efficient_llm.calls[0][1], 96)
        self.assertGreater(len(baseline_llm.calls[0][0][0]["content"]),
            len(efficient_llm.calls[0][0][0]["content"]))
        self.client.reads.clear()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.compare_runs(self.client, state_dir=self.state_dir)
        self.assertEqual(set(self.client.reads), set(baseline["trace_ids"] + efficient["trace_ids"]))
        self.assertIn("controlled", output.getvalue().lower())
        self.assertIn("baseline", output.getvalue())
        self.assertIn("efficient", output.getvalue())

    def test_ui_link_uses_saved_destination_and_prints_run_marker(self):
        report = self.run_suite()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_ui(report)
        self.assertIn("https://console.example.test/tenant/project/project/agent-streams/stream", output.getvalue())
        self.assertIn(report["run_id"], output.getvalue())

    def test_ui_links_to_the_stored_session_trace_and_model_span(self):
        report = self.run_suite()
        first = self.client.traces[report["trace_ids"][0]]
        first["session_id"] = "91cda0e7-0fcf-49e5-b899-eb71ad121d31"
        span_id = next(span["id"] for span in first["spans"] if span["type"] == "llm")
        report = workflow.report_findings(self.client, state_dir=self.state_dir)
        self.assertEqual(report["cases"][0]["session_id"], first["session_id"])
        self.assertEqual(report["cases"][0]["llm_span_id"], span_id)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_ui(report)
        self.assertIn(f"/sessions/{first['session_id']}?", output.getvalue())
        self.assertIn(f"traceId={first['id']}&tabView=conversation&spanId={span_id}", output.getvalue())

    def test_direct_ui_link_escapes_path_and_query_values(self):
        report = self.run_suite()
        row = report["cases"][0]
        row.update(session_id="session/one?injected=1", trace_id="trace&other=1", llm_span_id="span&extra=1")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            workflow.print_ui(report)
        links = [line.split(": ", 1)[1] for line in output.getvalue().splitlines()
            if line.startswith("Open first model span: ")]
        self.assertEqual(len(links), 1)
        url = urlsplit(links[0])
        self.assertTrue(url.path.endswith("/sessions/session%2Fone%3Finjected%3D1"))
        self.assertEqual(parse_qs(url.query), {"traceId": ["trace&other=1"],
            "tabView": ["conversation"], "spanId": ["span&extra=1"]})


if __name__ == "__main__":
    unittest.main()
