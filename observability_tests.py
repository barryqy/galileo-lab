from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import requests

from barrybot import DevNetLlmError
from galileo_client import GalileoApiError


QUESTIONS = [
    ("monitor", "Which three signals should I check when an AI assistant becomes slow?"),
    ("release", "Why should I test the same questions before changing an AI assistant?"),
    ("privacy", "How should a support assistant handle private customer information?"),
]
REFERENCE = (
    "Check request latency, usage, and errors. Compare a fixed set of questions before a release. "
    "Use fictional data in tests and keep private customer information out of unnecessary logs."
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
TOKEN_FIELDS = {"input_tokens": "num_input_tokens", "output_tokens": "num_output_tokens",
    "total_tokens": "num_total_tokens"}
AGENT_NAME = re.compile(r"(?:[A-Z][a-z]+){4}-[a-f0-9]{6}")


def get_agent_name(state_dir: Path) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    identity = state_dir / "agent.json"

    def read_name():
        try:
            fd = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"Cannot read agent identity: {identity}") from exc
        try:
            with os.fdopen(fd, encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Agent identity must be a regular file")
                payload = json.load(stream)
                name = payload.get("agent_name") if isinstance(payload, dict) else None
                if not isinstance(name, str) or len(name) > 64 or not AGENT_NAME.fullmatch(name):
                    raise ValueError("Saved agent identity is invalid; inspect agent.json before retrying")
                os.fchmod(stream.fileno(), 0o600)
                return name
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Saved agent identity is corrupt; inspect agent.json before retrying") from exc

    name = read_name()
    if name is not None:
        return name
    words = [secrets.choice(group) for group in (
        ("Calm", "Brisk", "Bright", "Quiet", "Silver", "Amber"),
        ("Cedar", "Maple", "Willow", "Pine", "Birch", "Aspen"),
        ("Otter", "Heron", "Badger", "Falcon", "Lynx", "Finch"),
        ("Trail", "River", "Cove", "Glade", "Meadow", "Grove"))]
    name = "".join(words) + "-" + secrets.token_hex(3)
    fd, tmp_path = tempfile.mkstemp(prefix=".agent-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"agent_name": name}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish a complete file without replacing an identity another process just saved.
            os.link(tmp_path, identity)
        except FileExistsError:
            return read_name()
        return name
    finally:
        os.unlink(tmp_path)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pii_labels(text):
    labels = []
    if EMAIL.search(text):
        labels.append("email")
    if PHONE.search(text):
        labels.append("phone")
    return labels


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _cases(suite):
    if suite in ("baseline", "efficient"):
        return QUESTIONS
    if suite == "privacy":
        return [
            ("fictional-contact", "This is fictional test data: learner@example.test and "
                "+1 202-555-0147 (a fake example phone number). Explain how a support assistant "
                "should handle it without repeating either contact value."),
            ("clean-control", "Explain how a support assistant should handle customer contact "
                "information. No customer contact details are included in this test."),
        ]
    raise ValueError("suite must be baseline, efficient, or privacy")


def _run_case(llm, suite, run_id, case, log_stream_id, agent_name):
    name, question = case
    trace_id = str(uuid.uuid4())
    created = _now()
    started = time.perf_counter_ns()
    metadata = {"run_id": run_id, "suite": suite, "case": name, "agent_name": agent_name,
        "pii_detector": "local-email-phone-pattern-v1", "synthetic_data": "true",
        "pii_input": ",".join(_pii_labels(question)) or "none"}
    tool_start = time.perf_counter_ns()
    time.sleep(0.4 if suite == "baseline" else 0.02)
    tool_ns = time.perf_counter_ns() - tool_start
    tool = {"id": str(uuid.uuid4()), "trace_id": trace_id, "name": "controlled-reference-retrieval",
        "type": "tool", "created_at": created, "input": question, "output": REFERENCE,
        "metrics": {"duration_ns": tool_ns}, "user_metadata": dict(metadata,
            timing_control="intentional-local-delay", delay_seconds="0.4" if suite == "baseline" else "0.02")}
    context = REFERENCE
    if suite == "baseline":
        context += " " + ("Additional background for this test: " + REFERENCE + " ") * 5
    messages = [{"role": "system", "content": "You are a concise support assistant. "
        "Do not repeat private contact details. Use this reference: " + context},
        {"role": "user", "content": question}]
    spans = [tool]
    status = "completed"
    try:
        answer = llm.complete(messages, max_tokens=180 if suite == "baseline" else 96)
        if not isinstance(answer, str) or not answer.strip():
            raise DevNetLlmError("The model returned an empty response")
        completion = dict(getattr(llm, "last_completion_metadata", {}) or {})
        metrics = {}
        for key in ("duration_ns", *TOKEN_FIELDS.values()):
            value = _number(completion.get(key))
            if value is not None:
                metrics[key] = value
        llm_metadata = dict(metadata, response_source="llm")
        for key in ("total_duration_ns", "request_count"):
            if _number(completion.get(key)) is not None:
                llm_metadata[key] = str(completion[key])
        spans.append({"id": str(uuid.uuid4()), "trace_id": trace_id, "name": "support-answer",
            "type": "llm", "created_at": completion.get("created_at") or _now(), "input": messages,
            "output": {"role": "assistant", "content": answer},
            "model": completion.get("model") or llm.model, "metrics": metrics,
            "user_metadata": llm_metadata})
        if isinstance(completion.get("finish_reason"), str) and completion["finish_reason"]:
            spans[-1]["finish_reason"] = completion["finish_reason"]
    except (DevNetLlmError, requests.RequestException) as exc:
        status = "model_error"
        answer = "Model request failed; no answer recorded."
        metadata["model_error_type"] = type(exc).__name__
    metadata.update(status=status, pii_output=",".join(_pii_labels(answer)) or "none")
    for span in spans:
        span["user_metadata"].update(pii_output=metadata["pii_output"], status=status)
    return {"id": trace_id, "name": f"{agent_name}-lab-{run_id}-{name}", "type": "trace", "created_at": created,
        "log_stream_id": log_stream_id, "input": question, "output": answer,
        "tags": ["devnet-test", agent_name, run_id, suite, name], "user_metadata": metadata,
        "status_code": 200 if status == "completed" else 503,
        "metrics": {"duration_ns": time.perf_counter_ns() - started}, "spans": spans}


def run_suite(client, ids: dict[str, str], llm, suite: str, *, state_dir: Path, console_url: str) -> dict:
    cases = _cases(suite)
    agent_name = get_agent_name(state_dir)
    run_id = str(uuid.uuid4())
    print(f"Your agent: {agent_name}", flush=True)
    print(f"Run {run_id} | {suite} | {len(cases)} tests", flush=True)
    traces = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case[0]}: retrieving reference, then calling the model...", flush=True)
        trace = _run_case(llm, suite, run_id, case, ids["log_stream_id"], agent_name)
        traces.append(trace)
        print(f"[{index}/{len(cases)}] {case[0]}: {trace['user_metadata']['status']}", flush=True)
    manifest = {"run_id": run_id, "suite": suite, "agent_name": agent_name,
        "created_at": _now(), "upload_status": "prepared",
        "destination": {"api_base_url": client.base_url.rstrip("/"), **ids,
            "console_url": console_url.rstrip("/")},
        "trace_ids": [trace["id"] for trace in traces],
        "cases": [{"trace_id": trace["id"], "case": trace["user_metadata"]["case"], "name": trace["name"],
            "span_ids": [span["id"] for span in trace["spans"]]}
            for trace in traces]}
    path = state_dir / "runs" / f"{run_id}.json"
    _write_json(path, manifest)
    _write_json(state_dir / "latest_run.json", {"run_id": run_id})
    print("Uploading traces to Galileo...", flush=True)
    result = client.post(f"/v2/projects/{ids['project_id']}/traces", json={"log_stream_id": ids["log_stream_id"],
        "traces": traces, "reliable": True, "include_trace_ids": True})
    if isinstance(result, dict):
        count = result.get("traces_count")
        if count is not None and (type(count) is not int or count != len(traces)):
            raise RuntimeError("Galileo upload accepted a different trace count; inspect this saved run before retrying.")
        returned_ids = result.get("trace_ids")
        if returned_ids is not None and (not isinstance(returned_ids, list)
                or any(not isinstance(value, str) for value in returned_ids)
                or len(returned_ids) != len(traces) or set(returned_ids) != set(manifest["trace_ids"])):
            raise RuntimeError("Galileo upload returned different trace IDs; inspect this saved run before retrying.")
    manifest["upload_status"] = "accepted"
    _write_json(path, manifest)
    print("Reading these exact trace IDs back from the Galileo API...", flush=True)
    return report_findings(client, state_dir=state_dir, run_id=run_id)


def _load_manifest(state_dir, run_id):
    if run_id is None:
        latest = state_dir / "latest_run.json"
        if not latest.exists():
            raise ValueError("No test run saved. Run run-tests first.")
        run_id = json.loads(latest.read_text(encoding="utf-8"))["run_id"]
    try:
        run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Invalid run ID") from exc
    path = state_dir / "runs" / f"{run_id}.json"
    if not path.exists():
        raise ValueError(f"No saved test run {run_id}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id:
        raise ValueError("Saved run ID does not match its manifest")
    if "agent_name" in manifest:
        name = manifest["agent_name"]
        if not isinstance(name, str) or len(name) > 64 or not AGENT_NAME.fullmatch(name):
            raise ValueError("Saved run agent identity is invalid")
    return manifest


def _fetch_trace(client, project_id, trace_id, span_ids):
    for attempt in range(5):
        try:
            trace = client.get(f"/v2/projects/{project_id}/traces/{trace_id}")
            if isinstance(trace, dict) and trace.get("id") and trace.get("spans"):
                stored_ids = {span.get("id") for span in trace["spans"]}
                if set(span_ids).issubset(stored_ids):
                    return trace
        except GalileoApiError as exc:
            if exc.status_code not in (404, 503):
                raise
        if attempt < 4:
            time.sleep(min(attempt + 1, 3))
    return None


def _labels(value):
    if value == "none":
        return []
    if not isinstance(value, str) or not value:
        return None
    labels = value.split(",")
    return labels if all(label in ("email", "phone") for label in labels) else None


def _case_findings(trace, saved, manifest):
    row = {"trace_id": saved["trace_id"], "case": saved["case"], "status": "pending",
        "model_ms": None, "model_wait_ms": None, "tool_ms": None, "total_ms": None,
        "input_tokens": None, "output_tokens": None, "total_tokens": None, "cost_usd": None,
        "pii_input": None, "pii_output": None, "request_count": None,
        "session_id": None, "llm_span_id": None, "finish_reason": None}
    if trace is None:
        return row
    metadata = trace.get("user_metadata") or {}
    agent_name = manifest.get("agent_name")
    if agent_name is not None and (metadata.get("agent_name") != agent_name
            or agent_name not in (trace.get("tags") or [])
            or trace.get("name") != f"{agent_name}-lab-{manifest['run_id']}-{saved['case']}"):
        raise ValueError("Galileo trace agent does not match the saved run")
    if (trace.get("id") != saved["trace_id"] or metadata.get("run_id") != manifest["run_id"]
            or metadata.get("suite") != manifest["suite"] or metadata.get("case") != saved["case"]):
        raise ValueError("Galileo trace does not match the saved run")
    stream = trace.get("log_stream_id")
    if stream is not None and stream != manifest["destination"]["log_stream_id"]:
        raise ValueError("Galileo trace destination does not match the saved run")
    for key, expected in (("project_id", manifest["destination"]["project_id"]),
            ("run_id", manifest["destination"]["log_stream_id"])):
        if trace.get(key) is not None and trace[key] != expected:
            raise ValueError("Galileo trace destination does not match the saved run")
    row.update(status=metadata.get("status", "unknown"), pii_input=_labels(metadata.get("pii_input")),
        pii_output=_labels(metadata.get("pii_output")), session_id=trace.get("session_id"))
    duration = _number((trace.get("metrics") or {}).get("duration_ns"))
    row["total_ms"] = duration / 1_000_000 if duration is not None else None
    tool_times = []
    model_costs = {}
    for span in trace.get("spans", []):
        span_metadata = span.get("user_metadata") or {}
        if agent_name is not None and span_metadata.get("agent_name") != agent_name:
            raise ValueError("Galileo span agent does not match the saved run")
        if (span_metadata.get("run_id") != manifest["run_id"]
                or span_metadata.get("suite") != manifest["suite"] or span_metadata.get("case") != saved["case"]):
            raise ValueError("Galileo span does not match the saved run")
        metrics = span.get("metrics") or {}
        duration = _number(metrics.get("duration_ns"))
        if span.get("type") == "tool":
            tool_times.append(duration)
        if span.get("type") != "llm":
            continue
        row["llm_span_id"] = row["llm_span_id"] or span.get("id")
        if isinstance(span.get("finish_reason"), str) and span["finish_reason"]:
            row["finish_reason"] = span["finish_reason"]
        cost = (span.get("metric_info") or {}).get("cost") or {}
        model_costs[span["id"]] = _number(cost.get("value")) if cost.get("status_type") == "success" else None
        row["model_ms"] = duration / 1_000_000 if duration is not None else None
        wait = span_metadata.get("total_duration_ns")
        if isinstance(wait, str) and wait.isdigit():
            row["model_wait_ms"] = int(wait) / 1_000_000
        count = span_metadata.get("request_count")
        if isinstance(count, str) and count.isdigit():
            row["request_count"] = int(count)
        for column, key in TOKEN_FIELDS.items():
            row[column] = _number(metrics.get(key))
    if tool_times and all(value is not None for value in tool_times):
        row["tool_ms"] = sum(tool_times) / 1_000_000
    if model_costs and all(value is not None for value in model_costs.values()):
        row["cost_usd"] = sum(model_costs.values())
    return row


def report_findings(client, *, state_dir: Path, run_id: str | None = None) -> dict:
    manifest = _load_manifest(state_dir, run_id)
    destination = manifest["destination"]
    if client.base_url.rstrip("/") != destination["api_base_url"]:
        raise ValueError("Current API destination differs from this saved run")
    rows = []
    for saved in manifest["cases"]:
        trace = _fetch_trace(client, destination["project_id"], saved["trace_id"], saved["span_ids"])
        rows.append(_case_findings(trace, saved, manifest))
    totals = {"completed_cases": sum(row["status"] == "completed" for row in rows), "cost_usd": None}
    for key in ("model_ms", "model_wait_ms", "tool_ms", "total_ms", "request_count", "cost_usd", *TOKEN_FIELDS):
        values = [row[key] for row in rows]
        totals[key] = sum(values) if all(value is not None for value in values) else None
    cost_reason = "Galileo cost is unavailable or not successfully computed. Provider tariff unavailable; no monetary cost is inferred from token counts."
    if totals["cost_usd"] is not None:
        cost_reason = "Sum of successful cost results on unique LLM spans; trace rollups are excluded."
    elif any(row["cost_usd"] is not None for row in rows):
        cost_reason = "Galileo cost is unavailable for some model spans; the total is unavailable. No tariff-based estimate is substituted."
    report = {"run_id": manifest["run_id"], "suite": manifest["suite"], "destination": destination,
        "trace_ids": manifest["trace_ids"], "cases": rows, "totals": totals, "retrieved_at": _now(),
        "api_verified": all(row["status"] != "pending" for row in rows),
        "pii_source": "Application code: local pattern check for email/phone, not Galileo PII classifier",
        "cost_source": "Galileo-reported model cost; not DevNet billing", "cost_reason": cost_reason}
    if "agent_name" in manifest:
        report["agent_name"] = manifest["agent_name"]
    _write_json(state_dir / "runs" / f"{manifest['run_id']}-findings.json", report)
    return report


def _display(value, suffix=""):
    if value is None:
        return "unavailable"
    return f"{value:.2f}{suffix}" if isinstance(value, float) else f"{value}{suffix}"


def _usd(value):
    return "unavailable" if value is None else f"${value:.6f}"


def print_findings(report: dict) -> None:
    if report.get("agent_name"):
        print(f"Your agent: {report['agent_name']}")
    print(f"Galileo API findings | {report['suite']} | run {report['run_id']}")
    print(f"Exact traces read back: {sum(row['status'] != 'pending' for row in report['cases'])}/{len(report['cases'])}")
    for row in report["cases"]:
        pii = "unavailable"
        if row["pii_input"] is not None and row["pii_output"] is not None:
            pii = f"input {','.join(row['pii_input']) or 'none'}, output {','.join(row['pii_output']) or 'none'}"
        print(f"  {row['case']}: {row['status']} | model {_display(row['model_ms'], ' ms')} | "
            f"tool {_display(row['tool_ms'], ' ms')} | tokens {_display(row['total_tokens'])} | "
            f"model cost {_usd(row['cost_usd'])} | PII {pii}")
        if row["finish_reason"] == "length":
            print("    Warning: response reached output limit; inspect completeness.")
    total = report["totals"]
    print(f"Totals: model {_display(total['model_ms'], ' ms')}, model with retries "
        f"{_display(total['model_wait_ms'], ' ms')}, controlled local tool {_display(total['tool_ms'], ' ms')}, "
        f"end-to-end {_display(total['total_ms'], ' ms')}")
    print(f"Tokens: input {_display(total['input_tokens'])}, output {_display(total['output_tokens'])}, "
        f"total {_display(total['total_tokens'])}")
    if any(row["request_count"] is not None and row["request_count"] > 1 for row in report["cases"]):
        print("Retries occurred. Tokens cover successful responses only; usage for failed attempts is unknown.")
    print(f"Cost: {_usd(total['cost_usd'])}. {report['cost_source']}. {report['cost_reason']}")
    print(f"PII: {report['pii_source']}. 'none' means this limited pattern found no match.")
    if not report["api_verified"]:
        print("Some traces are still unavailable from the API. Run findings again; cached measurements were not substituted.")
    _print_ui_links(report)


def _print_ui_links(report: dict) -> None:
    destination = report["destination"]
    url = (f"{destination['console_url']}/project/{quote(destination['project_id'], safe='')}/"
        f"agent-streams/{quote(destination['log_stream_id'], safe='')}")
    print(f"Galileo UI: {url}")
    first = report["cases"][0] if report["cases"] else {}
    if first.get("session_id") and first.get("llm_span_id"):
        query = urlencode({"traceId": first["trace_id"], "tabView": "conversation", "spanId": first["llm_span_id"]})
        print(f"Open first model span: {url}/sessions/{quote(first['session_id'], safe='')}?{query}")


def print_ui(report: dict) -> None:
    if report.get("agent_name"):
        print(f"Your agent: {report['agent_name']}")
    _print_ui_links(report)
    print(f"Find this run marker in trace names: {report['run_id']}")
    print("Use the run time range, open the same trace and LLM span, and compare the API values.")
    for row in report["cases"]:
        print(f"  {row['case']}: {row['trace_id']}")


def compare_runs(client, *, state_dir: Path) -> None:
    chosen = {}
    for path in sorted((state_dir / "runs").glob("*.json"), reverse=True):
        if path.name.endswith("-findings.json"):
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        suite = manifest.get("suite")
        if (suite not in ("baseline", "efficient") or manifest.get("upload_status") != "accepted"
                or manifest["destination"]["api_base_url"] != client.base_url.rstrip("/")):
            continue
        if suite not in chosen or manifest["created_at"] > chosen[suite]["created_at"]:
            chosen[suite] = manifest
    if len(chosen) != 2:
        raise ValueError("Run both baseline and efficient suites against this destination before comparing.")
    if chosen["baseline"]["destination"] != chosen["efficient"]["destination"]:
        raise ValueError("The baseline and efficient runs have different destinations; rerun both in one workspace.")
    if chosen["baseline"].get("agent_name") != chosen["efficient"].get("agent_name"):
        raise ValueError("The baseline and efficient runs have different agent identities; rerun both for the same agent.")
    print("Baseline / efficient comparison from fresh API reads of saved trace IDs")
    for suite in ("baseline", "efficient"):
        report = report_findings(client, state_dir=state_dir, run_id=chosen[suite]["run_id"])
        print_findings(report)
    print("The local retrieval delay is controlled: baseline 0.40 s/case, efficient 0.02 s/case. "
        "Model timing still varies; this comparison does not establish equal answer quality.")
