# Galileo DevNet Lab Helper

Run support tests, inspect token use, delay, and fictional private-data findings through the API, and find the same records in Galileo.

## Quick Start

An instructor-enabled lab session is required. Use the setup block in the DevNet guide or this block in the configured lab environment:

```bash
cd /home/developer/src || exit 1
if [ ! -d galileo-lab ]; then git clone https://github.com/barryqy/galileo-lab.git || exit 1; fi
cd /home/developer/src/galileo-lab || exit 1
git pull --ff-only || exit 1
if [ -n "${VIRTUAL_ENV:-}" ] && type deactivate >/dev/null 2>&1; then deactivate; fi
python3 -m pip install uv --disable-pip-version-check || exit 1
if [ ! -x .venv/bin/python ]; then python3 -m uv venv --python 3.12 .venv || exit 1; fi
python3 -m uv pip install --python .venv/bin/python -r requirements.txt || exit 1
source 0-init-lab.sh workshop || exit 1
python3 galileo_lab.py run-tests --suite baseline
```

Watch the cases run. The first batch assigns a random agent name and reuses it for later batches in this lab session. Keep the agent name and run ID, then read the saved results before opening the UI:

```bash
cd /home/developer/src/galileo-lab || exit 1
source 0-init-lab.sh workshop >/dev/null || exit 1
python3 galileo_lab.py findings || exit 1
python3 galileo_lab.py open-ui
```

Sign in to Workshop with your own instructor-provided Viewer account. Open the displayed stream link, search for your agent name, then select the exact run and trace IDs shown by the API. Classmates can see the same synthetic traces; agent names help you find results and do not restrict access. Learners without UI access can still complete the API exercises.

Run the efficient batch next, followed by `findings`, `compare-tests`, and, with viewer access, `open-ui`. Then run the privacy batch and inspect it with `findings` and `open-ui`. These read commands do not make another model call.

| Command | Purpose |
| --- | --- |
| `agent-name` | Show the persistent name assigned to this lab environment; no API login required |
| `run-tests --suite baseline` | Run initial support cases and save evidence |
| `run-tests --suite efficient` | Reduce repeated context and limit answer length on the same cases |
| `run-tests --suite privacy` | Exercise a local email/phone check with fictional data |
| `findings` | Read the latest test run through the API |
| `findings --run UUID` | Read a selected saved test run |
| `compare-tests` | Compare the latest saved baseline and efficient batches |
| `open-ui` | Display the latest test run's stream location and identifiers |
| `open-ui --run UUID` | Display a selected run's location and identifiers |

### Reading Measurements

- Token counts come from provider usage. Missing usage stays unavailable.
- Model cost is Galileo-reported when computed. Missing or pending cost stays unavailable; the reported value is not actual DevNet billing. The helper does not infer cost from a hardcoded tariff.
- Model time and application time are separate. The baseline's controlled local retrieval delay demonstrates application waiting, not Galileo network latency.
- The privacy batch supplies fictional contact data and a clean control. Its email/phone rule detects patterns in input/output; it does not redact or block. It is not a Galileo PII scorer and does not cover every form of sensitive information.
- Match your agent name and exact run/trace IDs in API/UI comparisons. Check aggregation scope and units before comparing a chart with one API row.
- Agent names are assigned once per local state directory and are retained across baseline, efficient, and privacy batches. Earlier saved runs without names remain readable.

## Instructor Configuration

The guide uses `source 0-init-lab.sh workshop` for the Workshop destination:

| Setting | Destination |
| --- | --- |
| Galileo API | `https://api.multitenant.galileocloud.io` |
| Workshop session API | Selected automatically from the DevNet environment |
| Console | `https://console.multitenant.galileocloud.io/workshop` |
| Project | **AI Observability Lab**, `2e652072-412e-4bef-97a5-1a8f4dc5b68e` |
| Stream | **ai-observability-lab**, `9ac1ae85-1c47-4447-8050-df5fbb5b2ae6` |
| Local state | `.galileo/workshop` |

Workshop initialization obtains a temporary session using the running DevNet environment. Learners do not need a Galileo API key. The session expires when the environment ends; initialization stops if access is unavailable. Rerun `source 0-init-lab.sh workshop` to reconnect.

Configure `GALILEO_LAB_BOOTSTRAP_URL` as `https://devnet.cisco.com/v1/devenv-community/galileo/sessions` (or the corresponding `devnet-testing.cisco.com` URL). Existing environments can select the matching service from their public `LLM_BASE_URL`. Bootstrap uses the environment's existing `DEVENV_USER`, `DEVENV_PASSWORD`, and `devpod-` hostname. Session credentials stay in separate `GALILEO_LAB_SESSION_*` fields and `GALILEO_LAB_GATEWAY_URL`; they are never used with the native Galileo API.

Preloaded `GALILEO_API_KEY` and instructor-configured `GALILEO_WORKSHOP_KEY_SEED_URL` remain supported and take precedence over temporary sessions, retaining the native Galileo destination. A native key with an explicit native `GALILEO_API_BASE_URL` can also read earlier saved runs from that destination. Saved runs remain bound to their original API destination. Do not use the older `barry-2` default seed or the SplunkSE observability seed for Workshop. Stop on initialization failure. Do not store raw credentials in this repository.

Enable the Workshop runtime credential for learner sessions before offering the exercises. Give each learner an individual organization Read-only identity with Viewer access to AI Observability Lab, and verify sign-in separately. A Viewer login permits UI exploration; it does not supply the runtime ingestion credential. The helper does not create identities or grants. Learners without viewer access can complete the API path once their API session is enabled.

Use an individual UI identity for each learner so they can keep their own signed-in session.

Before the workshop, check a fresh learner environment: the first play action should complete a baseline batch, and the learner should be able to open AI Observability Lab with their Viewer account.

The older profiles remain supported with their existing settings:

| Profile | Destination |
| --- | --- |
| `source 0-init-lab.sh` | Original `barry-2` destination |
| `source 0-init-lab.sh observability` | SplunkSE console `https://console.multitenant.galileocloud.io/splunkse`; **AI Observability Lab** project `59f3d09d-c104-4f40-a3b8-77af51ea55d1`; **ai-observability-lab** stream `3d95e2c0-38cd-4c1a-bdd8-7a259de8a33b` |

The SplunkSE profile retains its preloaded `GALILEO_API_KEY` or `GALILEO_OBSERVABILITY_KEY_SEED_URL` configuration. Select the intended profile before running commands; the tenants do not share projects or credentials.

## Optional Evaluation and Agent Exercises

All original commands remain available. They use a separate refund scenario with simulated tools, application metrics, and live model summaries where requested.

| Command | Purpose |
| --- | --- |
| `env` | Inspect the configured API session |
| `llm-check` | Require a live response from the lab model |
| `setup` | Create or reuse the configured project and stream |
| `agent-demo` | Record both refund paths with live model summaries |
| `trace-payload` | Inspect an example trace |
| `log-traces` | Record the original trace examples |
| `query-traces` | Query the stream, potentially including other runs |
| `barrybot --ask "QUESTION"` | Ask BarryBot a question and record it |
| `dataset-preview` | Inspect the four refund cases |
| `dataset [--name NAME]` | Create or reuse the refund dataset |
| `experiment [--name NAME]` | Replay both refund variants and upload metrics |
| `release-gate` | Apply local thresholds to the saved report |
| `release-gate --variant candidate` | Return success only when the candidate passes |
| `release-gate --variant baseline` | Demonstrate the baseline's nonzero failure exit |
| `guardrail [--prompt TEXT]` | Check the local credential rule before a model call |
| `human-workflows` | Attach sample ratings and annotations to refund traces |
| `dashboard` | Configure refund release-metric widgets |
| `trends` | Inspect stored trend sections and widgets |
| `scorers` | List visible Galileo scorers |
| `integrations` | List visible integrations |
| `capabilities` | Inspect the bundled API catalog |
| `outcomes` | Inspect the capability-to-outcome mapping |

Run the refund workflow in this order after initializing the intended profile:

```bash
cd /home/developer/src/galileo-lab || exit 1
source 0-init-lab.sh workshop >/dev/null || exit 1
python3 galileo_lab.py agent-demo || exit 1
python3 galileo_lab.py dataset-preview || exit 1
python3 galileo_lab.py dataset || exit 1
python3 galileo_lab.py experiment || exit 1
python3 galileo_lab.py release-gate || exit 1
python3 galileo_lab.py guardrail || exit 1
python3 galileo_lab.py human-workflows || exit 1
python3 galileo_lab.py dashboard || exit 1
python3 galileo_lab.py trends
```

Refund experiments replay simulated tools without additional model calls. The helper calculates and uploads their application metrics. The gate reads the saved report and applies local thresholds. Galileo scorers must be enabled separately.

The Agent Control credential-term regex allows the safe model call once and denies a matching request with zero protected calls. Both decisions are logged. Benign requests containing its terms can match and paraphrases can pass. This is separate from the email/phone check, not a general PII detector.

`llm-check` requires a live response. Original interactive and trace examples retain a labeled local fallback after model failures; that response is not recorded as a model span.

Human-review commands record sample ratings rather than independent judgments. A saved widget definition does not prove a chart has data. Check metric availability, span/trace aggregation, and the time range.

## Files

- `0-init-lab.sh` selects the destination and prepares the session.
- `barrybot.py` calls the model supplied by the lab.
- `galileo_client.py` provides the Galileo REST client.
- `galileo_lab.py` provides the CLI commands.
- `support_agent.py` defines simulated refund paths, application metrics, and traces.
- `samples/eval_cases.csv` contains refund evaluation cases.
- `agent_control_demo.py` defines the local credential control.
- `data/galileo_api_capabilities.json` contains the API catalog.
