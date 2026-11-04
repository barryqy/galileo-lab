import csv
import json
from pathlib import Path


LEGACY_OUTPUTS = {
    "Refund order ORD-3011. My account is verified and it arrived yesterday.":
        "Issue the refund after verifying the order and policy.",
}
DATASET_CONFLICT = "Existing dataset differs from the lab cases; choose a separate --name."


def _checked_rows(content, expected):
    if not isinstance(content, dict):
        raise RuntimeError(DATASET_CONFLICT)
    rows = content.get("rows")
    if not isinstance(rows, list) or len(rows) != 4 or content.get("paginated"):
        raise RuntimeError(DATASET_CONFLICT)

    found = {}
    bundled_inputs = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("values_dict"), dict):
            raise RuntimeError(DATASET_CONFLICT)
        values = row.get("values_dict") or {}
        prompt = values.get("input")
        if not isinstance(prompt, str):
            raise RuntimeError(DATASET_CONFLICT)
        if prompt not in expected:
            try:
                bundled = json.loads(prompt)
            except ValueError as exc:
                raise RuntimeError(DATASET_CONFLICT) from exc
            prompt = bundled.get("input") if isinstance(bundled, dict) else None
            if not isinstance(prompt, str) or prompt not in expected or "output" not in values or values["output"] is not None:
                raise RuntimeError(DATASET_CONFLICT)
            if bundled != {"input": prompt, "ground_truth": expected[prompt]["ground_truth"]}:
                raise RuntimeError(DATASET_CONFLICT)
            bundled_inputs.add(prompt)
        elif values.get("output" if "output" in values else "ground_truth") != expected[prompt]["ground_truth"]:
            raise RuntimeError(DATASET_CONFLICT)
        if prompt in found:
            raise RuntimeError(DATASET_CONFLICT)

        metadata = values.get("metadata")
        if metadata:
            try:
                metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                case_id = metadata.get("case_id")
                expected_id = json.loads(expected[prompt]["metadata"])["case_id"]
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                raise RuntimeError(DATASET_CONFLICT) from exc
            if case_id is not None and case_id != expected_id:
                raise RuntimeError(DATASET_CONFLICT)
        found[prompt] = row
    return found, bundled_inputs


def reconcile_dataset(client, dataset_id, csv_path):
    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        cases = list(csv.DictReader(handle))
    expected = {row["input"]: row for row in cases}
    if len(cases) != 4 or len(expected) != 4:
        raise RuntimeError("The lab CSV must contain four distinct evaluation cases")

    path = f"/v2/datasets/{dataset_id}"
    rows, bundled_inputs = _checked_rows(client.get(f"{path}/content"), expected)
    response_headers = getattr(client, "last_response_headers", {})
    etag = next((value for key, value in response_headers.items() if key.lower() == "etag"), None)
    edits = []
    intended = {prompt: dict(row["values_dict"]) for prompt, row in rows.items()}
    for prompt, row in rows.items():
        changes = {}
        if prompt in bundled_inputs:
            changes = {"input": prompt, "output": expected[prompt]["ground_truth"]}
        current = row["values_dict"].get("generated_output")
        correct = expected[prompt]["generated_output"]
        if current != correct:
            if prompt not in LEGACY_OUTPUTS or current != LEGACY_OUTPUTS[prompt]:
                raise RuntimeError(DATASET_CONFLICT)
            changes["generated_output"] = correct
        if not changes:
            continue
        if not row.get("row_id"):
            raise RuntimeError(DATASET_CONFLICT)
        # Galileo replaces the row, so every untouched value must travel with the edit.
        intended[prompt].update(changes)
        edits.append({"edit_type": "update_row", "row_id": row["row_id"], "values": intended[prompt]})

    if edits:
        headers = {"If-Match": etag} if etag else {}
        client.patch(f"{path}/content", json={"edits": edits}, headers=headers)
        verified, still_bundled = _checked_rows(client.get(f"{path}/content"), expected)
        if still_bundled:
            raise RuntimeError("Could not verify the dataset input correction")
        for prompt, row in verified.items():
            if row["values_dict"] != intended[prompt]:
                raise RuntimeError("Could not verify the dataset correction")
            if row.get("row_id") != rows[prompt].get("row_id"):
                raise RuntimeError("Could not verify preserved dataset row IDs")

    return client.get(path)
