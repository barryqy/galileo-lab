import copy
import csv
import json
import unittest
from pathlib import Path

from dataset_sync import reconcile_dataset


CSV_PATH = Path(__file__).resolve().parents[1] / "samples" / "eval_cases.csv"
LEGACY_INPUT = "Refund order ORD-3011. My account is verified and it arrived yesterday."
LEGACY_OUTPUT = "Issue the refund after verifying the order and policy."


class DatasetClient:
    def __init__(self):
        with CSV_PATH.open(encoding="utf-8") as handle:
            self.rows = [
                {"row_id": f"row-{i}", "values_dict": row}
                for i, row in enumerate(csv.DictReader(handle))
            ]
        self.version = 1
        self.last_response_headers = {}
        self.patches = []
        self.ignore_edit = False
        self.drop_field = None
        self.etag = '"version-1"'

    def get(self, path):
        if path.endswith("/content"):
            self.last_response_headers = {"etag": self.etag} if self.etag else {}
            return {"rows": copy.deepcopy(self.rows), "paginated": False}
        return {"id": "dataset", "current_version_index": self.version, "num_rows": len(self.rows)}

    def patch(self, path, **kwargs):
        self.patches.append((path, kwargs))
        if not self.ignore_edit:
            for edit in kwargs["json"]["edits"]:
                row = next(row for row in self.rows if row["row_id"] == edit["row_id"])
                row["values_dict"] = copy.deepcopy(edit["values"])
                if self.drop_field:
                    row["values_dict"].pop(self.drop_field, None)
        self.version += 1
        return {}

    def legacy_row(self):
        row = next(row for row in self.rows if row["values_dict"]["input"] == LEGACY_INPUT)
        row["values_dict"]["generated_output"] = LEGACY_OUTPUT
        return row

    def bundled_legacy_rows(self):
        self.legacy_row()
        for row in self.rows:
            values = row["values_dict"]
            values["input"] = json.dumps({
                "input": values["input"], "ground_truth": values.pop("ground_truth"),
            })
            values["output"] = None

    def canonical_rows(self):
        for row in self.rows:
            values = row["values_dict"]
            values["output"] = values.pop("ground_truth")


class DatasetSyncTests(unittest.TestCase):
    def test_current_dataset_is_a_noop(self):
        client = DatasetClient()
        result = reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(client.patches, [])
        self.assertEqual(result["current_version_index"], 1)

    def test_only_known_legacy_cell_is_changed_with_optimistic_header(self):
        client = DatasetClient()
        row = client.legacy_row()
        row["values_dict"]["review_note"] = "Preserve this reviewed context."
        original_values = copy.deepcopy(row["values_dict"])
        old_ids = [item["row_id"] for item in client.rows]
        result = reconcile_dataset(client, "dataset", CSV_PATH)

        self.assertEqual(result["id"], "dataset")
        self.assertEqual(result["current_version_index"], 2)
        self.assertEqual([item["row_id"] for item in client.rows], old_ids)
        path, kwargs = client.patches[0]
        self.assertEqual(path, "/v2/datasets/dataset/content")
        self.assertEqual(kwargs["headers"], {"If-Match": '"version-1"'})
        self.assertEqual(kwargs["json"]["edits"], [{
            "edit_type": "update_row", "row_id": row["row_id"],
            "values": {**original_values, "generated_output": row["values_dict"]["generated_output"]},
        }])
        for field in original_values:
            if field != "generated_output":
                self.assertEqual(row["values_dict"][field], original_values[field])
        reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(len(client.patches), 1)

    def test_unfamiliar_output_is_never_overwritten(self):
        for output in ("A learner's reviewed answer.", None):
            with self.subTest(output=output):
                client = DatasetClient()
                client.legacy_row()["values_dict"]["generated_output"] = output
                with self.assertRaisesRegex(RuntimeError, "--name"):
                    reconcile_dataset(client, "dataset", CSV_PATH)
                self.assertEqual(client.patches, [])

    def test_missing_or_mismatched_cases_are_rejected(self):
        for change in ("missing", "extra", "duplicate", "input", "ground_truth", "case_id"):
            with self.subTest(change=change):
                client = DatasetClient()
                if change == "missing":
                    client.rows.pop()
                elif change == "extra":
                    client.rows.append(copy.deepcopy(client.rows[0]))
                elif change == "duplicate":
                    client.rows[-1] = copy.deepcopy(client.rows[0])
                elif change == "case_id":
                    client.rows[0]["values_dict"]["metadata"] = json.dumps({"case_id": "other"})
                else:
                    client.rows[0]["values_dict"][change] = "different"
                with self.assertRaisesRegex(RuntimeError, "--name"):
                    reconcile_dataset(client, "dataset", CSV_PATH)
                self.assertEqual(client.patches, [])

    def test_update_requires_row_id(self):
        client = DatasetClient()
        del client.legacy_row()["row_id"]
        with self.assertRaisesRegex(RuntimeError, "--name"):
            reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(client.patches, [])

    def test_update_is_verified_before_success(self):
        client = DatasetClient()
        client.legacy_row()
        client.ignore_edit = True
        with self.assertRaisesRegex(RuntimeError, "verify"):
            reconcile_dataset(client, "dataset", CSV_PATH)

    def test_readback_detects_dropped_metadata(self):
        client = DatasetClient()
        client.legacy_row()
        client.drop_field = "metadata"
        with self.assertRaisesRegex(RuntimeError, "verify"):
            reconcile_dataset(client, "dataset", CSV_PATH)

    def test_missing_optional_etag_does_not_invent_one(self):
        client = DatasetClient()
        client.legacy_row()
        client.etag = None
        reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(client.patches[0][1]["headers"], {})

    def test_canonical_output_column_is_a_noop(self):
        client = DatasetClient()
        client.canonical_rows()
        result = reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(result["current_version_index"], 1)
        self.assertEqual(client.patches, [])

    def test_legacy_bundled_inputs_are_repaired_without_replacing_rows(self):
        client = DatasetClient()
        client.rows[0]["values_dict"]["review_note"] = {"owner": "reviewer", "retain": 1}
        original = copy.deepcopy(client.rows)
        client.bundled_legacy_rows()
        result = reconcile_dataset(client, "dataset", CSV_PATH)

        self.assertEqual(result["current_version_index"], 2)
        edits = client.patches[0][1]["json"]["edits"]
        self.assertEqual(len(edits), 4)
        self.assertEqual(client.patches[0][1]["headers"], {"If-Match": '"version-1"'})
        for before, after, edit in zip(original, client.rows, edits):
            self.assertEqual(after["row_id"], before["row_id"])
            self.assertEqual(after["values_dict"]["input"], before["values_dict"]["input"])
            self.assertEqual(after["values_dict"]["output"], before["values_dict"]["ground_truth"])
            self.assertEqual(after["values_dict"]["generated_output"], before["values_dict"]["generated_output"])
            self.assertEqual(after["values_dict"]["metadata"], before["values_dict"]["metadata"])
            expected_values = dict(before["values_dict"])
            expected_values["output"] = expected_values.pop("ground_truth")
            self.assertEqual(edit["values"], expected_values)
            self.assertEqual(after["values_dict"], expected_values)
        reconcile_dataset(client, "dataset", CSV_PATH)
        self.assertEqual(len(client.patches), 1)

    def test_unknown_bundled_shape_is_never_rewritten(self):
        for change in ("input", "ground_truth", "extra", "output"):
            with self.subTest(change=change):
                client = DatasetClient()
                client.bundled_legacy_rows()
                values = client.rows[0]["values_dict"]
                bundled = json.loads(values["input"])
                if change == "output":
                    values["output"] = "A learner's expected answer."
                else:
                    bundled[change] = "unrecognized"
                    values["input"] = json.dumps(bundled)
                with self.assertRaisesRegex(RuntimeError, "--name"):
                    reconcile_dataset(client, "dataset", CSV_PATH)
                self.assertEqual(client.patches, [])


if __name__ == "__main__":
    unittest.main()
