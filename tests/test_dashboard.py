import argparse
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import galileo_lab


class DashboardClient:
    base_url = "https://api.example.test"

    def __init__(self, trends, discard_writes=False):
        self.trends = copy.deepcopy(trends)
        self.discard_writes = discard_writes
        self.created = []

    def get(self, path):
        if path != "/v2/projects/project/log_streams/stream/trends":
            raise AssertionError(path)
        return copy.deepcopy(self.trends)

    def post(self, path, json):
        if path != "/v2/projects/project/log_streams/stream/trends/widgets":
            raise AssertionError(path)
        widget = {"id": f"created-{len(self.created)}", **json}
        self.created.append(widget)
        if not self.discard_writes:
            self.trends["sections"][-1]["widgets"].append(widget)
        return copy.deepcopy(widget)


def widget(name, metric, aggregation, section_id, widget_id):
    return {"id": widget_id, "name": name, "description": None,
        "type": "line_chart", "dataset": None, "metric": metric,
        "aggregation": aggregation, "section_id": section_id}


def release_widgets():
    return [
        widget("Action completion", "action_completion", "Average", "release", "action"),
        widget("Policy compliance", "policy_compliance", "Average", "release", "policy"),
        widget("Tool selection quality", "tool_selection_quality", "Average", "release", "selection"),
        widget("Tool errors", "tool_errors", "Sum", "release", "errors"),
    ]


def trends_fixture():
    return {"id": "trends", "widgets": [], "sections": [
        {"id": "quality", "name": "Agent Quality", "widgets": [
            widget("Tool Selection Quality", "native-scorer", "Count", "quality", "native-selection")
        ]},
        {"id": "custom", "name": "Custom Metrics", "widgets": [
            widget("Action Completion", "action_completion", "Average", "custom", "custom-action"),
            widget("Policy Compliance", "policy_compliance", "Average", "custom", "custom-policy"),
            widget("Tool Errors", "tool_errors", "Average", "custom", "custom-errors"),
        ]},
        {"id": "release", "name": "BarryBot release readiness", "widgets": []},
    ]}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = patch.object(galileo_lab, 'STATE_DIR', Path(tmp.name))
        state.start()
        self.addCleanup(state.stop)

    def run_dashboard(self, client):
        output = io.StringIO()
        ids = {"project_id": "project", "log_stream_id": "stream"}
        with patch.object(galileo_lab, "client_or_exit", return_value=client):
            with patch.object(galileo_lab, "ensure_project_and_stream", return_value=ids):
                with contextlib.redirect_stdout(output):
                    galileo_lab.cmd_dashboard(argparse.Namespace())
        return output.getvalue()

    def test_other_sections_cannot_suppress_release_widgets(self):
        client = DashboardClient(trends_fixture())
        unrelated = copy.deepcopy(client.trends["sections"][:-1])
        output = self.run_dashboard(client)
        stored = client.trends["sections"][-1]["widgets"]
        self.assertEqual([(w["metric"], w["aggregation"]) for w in stored], [
            ("action_completion", "Average"), ("policy_compliance", "Average"),
            ("tool_selection_quality", "Average"), ("tool_errors", "Sum"),
        ])
        self.assertEqual(client.trends["sections"][:-1], unrelated)
        self.assertIn("release widgets: 4", output)
        self.run_dashboard(client)
        self.assertEqual(len(client.created), 4)

    def test_top_level_widgets_are_matched_and_counted_by_section_id(self):
        trends = trends_fixture()
        trends["widgets"] = release_widgets()
        client = DashboardClient(trends)
        output = self.run_dashboard(client)
        self.assertEqual(client.created, [])
        self.assertIn("release widgets: 4", output)
        self.assertIn("total widgets: 8", output)

    def test_same_name_with_wrong_mapping_does_not_suppress_correct_widget(self):
        trends = trends_fixture()
        trends["sections"][-1]["widgets"] = release_widgets()
        trends["sections"][-1]["widgets"][-1]["aggregation"] = "Average"
        client = DashboardClient(trends)
        self.run_dashboard(client)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["metric"], "tool_errors")
        self.assertEqual(client.created[0]["aggregation"], "Sum")
        self.assertEqual(client.trends["sections"][-1]["widgets"][-2]["aggregation"], "Average")

    def test_missing_readback_mapping_fails_without_claiming_configuration(self):
        client = DashboardClient(trends_fixture(), discard_writes=True)
        output = io.StringIO()
        ids = {"project_id": "project", "log_stream_id": "stream"}
        with patch.object(galileo_lab, "client_or_exit", return_value=client):
            with patch.object(galileo_lab, "ensure_project_and_stream", return_value=ids):
                with patch("sys.argv", ["galileo_lab.py", "dashboard"]):
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(galileo_lab.main(), 1)
        self.assertIn("widgets missing after readback: Action completion", output.getvalue())
        self.assertNotIn("dashboard configured", output.getvalue())


if __name__ == "__main__":
    unittest.main()
