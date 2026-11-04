import unittest
from unittest.mock import Mock, patch

from galileo_client import GalileoClient


class ClientTests(unittest.TestCase):
    def test_dataset_cell_named_user_metadata_keeps_its_original_types(self):
        response = Mock(status_code=204, text="", headers={})
        payload = {"edits": [{"edit_type": "update_row", "row_id": "row",
            "values": {"input": "Known case", "user_metadata": {"revision": 2}}}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").patch("/v2/datasets/dataset/content", json=payload)
        self.assertEqual(request.call_args.kwargs["json"], payload)
        self.assertIsInstance(request.call_args.kwargs["json"]["edits"][0]["values"]["user_metadata"]["revision"], int)

    def test_application_input_metadata_is_not_treated_as_trace_metadata(self):
        response = Mock(status_code=200, text="{}", headers={})
        response.json.return_value = {}
        payload = {"traces": [{"input": {"user_metadata": {"attempt": 2}},
            "user_metadata": {"executed": True}}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").post("/v2/projects/project/traces", json=payload)
        sent = request.call_args.kwargs["json"]["traces"][0]
        self.assertEqual(sent["input"], payload["traces"][0]["input"])
        self.assertEqual(sent["user_metadata"], {"executed": "true"})

    def test_metadata_uses_api_strings_without_changing_metrics_or_outputs(self):
        response = Mock(status_code=200, text="{}")
        response.headers = {"ETag": "dataset-version"}
        response.json.return_value = {}
        payload = {"traces": [{"user_metadata": {"executed": False, "calls": 0, "model": None},
            "metrics": {"completion": 0.0}, "spans": [{"user_metadata": {"used_fallback": True},
                "output": {"approved": False}}]}]}
        with patch("galileo_client.requests.request", return_value=response) as request:
            GalileoClient(api_key="provided by the runtime").post("/v2/projects/project/traces", json=payload)
        sent = request.call_args.kwargs["json"]["traces"][0]
        self.assertEqual(sent["user_metadata"], {"executed": "false", "calls": "0"})
        self.assertEqual(sent["spans"][0]["user_metadata"], {"used_fallback": "true"})
        self.assertEqual(sent["metrics"], {"completion": 0.0})
        self.assertEqual(sent["spans"][0]["output"], {"approved": False})
        self.assertFalse(payload["traces"][0]["user_metadata"]["executed"])


if __name__ == "__main__":
    unittest.main()
