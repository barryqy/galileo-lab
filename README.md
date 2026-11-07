# Splunk Observability DevNet Lab Helper

Run support tests, inspect token use, delay, and fictional private-data findings through the API, and find the same records in Splunk Observability.

## Quick Start

Click the play button in the guide to prepare the lab and run the tests. Setup connects automatically, and later commands reuse the session. Use the setup block in the DevNet guide or this block in the configured lab environment:

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

After setup, run `player` anytime to show your saved agent name in bold green, including from another directory. It reuses the current lab profile and needs no API login or model call. `python3 galileo_lab.py player` also works from the lab directory.

Sign in to Workshop with your own instructor-provided Viewer account. Open the displayed stream link, search for your agent name, then select the exact run and trace IDs shown by the API. Classmates can see the same synthetic traces; agent names help you find results and do not restrict access. Learners without UI access can still complete the API exercises.

Run the efficient batch next, followed by `findings`, `compare-tests`, and, with viewer access, `open-ui`. Then run the privacy batch and inspect it with `findings` and `open-ui`. These read commands do not make another model call.

| Command | Purpose |
| --- | --- |
| `player` | Show the same persistent agent name in bold green; available in the shell after setup |
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
- Model cost is Splunk Observability-reported when computed. Missing or pending cost stays unavailable; the reported value is not actual DevNet billing. The helper does not infer cost from a hardcoded tariff.
- Model time and application time are separate. The baseline's controlled local retrieval delay demonstrates application waiting, not Splunk Observability network latency.
- The privacy batch supplies fictional contact data and a clean control. Its email/phone rule detects patterns in input/output; it does not redact or block. It is not a Splunk Observability PII scorer and does not cover every form of sensitive information.
- Match your agent name and exact run/trace IDs in API/UI comparisons. Check aggregation scope and units before comparing a chart with one API row.
- Agent names are assigned once per local state directory and are retained across baseline, efficient, and privacy batches. Earlier saved runs without names remain readable.

## Instructor Configuration

The guide uses `source 0-init-lab.sh workshop` for the Workshop destination:

| Setting | Destination |
| --- | --- |
| Splunk Observability API | `https://api.multitenant.galileocloud.io` |
| Workshop session API | `https://o11y.barrysecure.com/v1/galileo/proxy` |
| Console | `https://console.multitenant.galileocloud.io/workshop` |
| Project | **AI Observability Lab**, `2e652072-412e-4bef-97a5-1a8f4dc5b68e` |
| Stream | **ai-observability-lab**, `9ac1ae85-1c47-4447-8050-df5fbb5b2ae6` |
| Local state | `.galileo/workshop` |

Workshop initialization connects through the Barrysecure gateway. The CLI obtains its temporary session automatically when the learner clicks play; no webpage or link is needed. No Splunk Observability API key, Cisco password or model-service credential is sent to Barrysecure. API access tokens last up to one hour and renew automatically while the workshop session remains valid. New code-free sessions last up to three hours and cannot extend beyond the configured workshop window. Re-sourcing `source 0-init-lab.sh workshop` checks and renews the existing connection. If the session has expired, run setup again while the workshop is open. Existing browser and invitation modes remain available as alternatives when automatic admission is unavailable.

The temporary API session and the Workshop Viewer login are separate. Setup enables the test commands; use your own Workshop Viewer account to inspect the same results in Splunk Observability.

Session credentials stay in `GALILEO_LAB_SESSION_TOKEN` and `GALILEO_LAB_SESSION_RENEWAL_TOKEN`, with the destination in `GALILEO_LAB_GATEWAY_URL`. They are never used with the native Splunk Observability API. The helper does not save credentials to disk. A failed command can renew an established connection once; it never starts another interactive Connect flow. If an upload cannot be replayed safely, reopen its source and rerun the command.

The existing DevNet gateway remains available through explicit `GALILEO_LAB_BOOTSTRAP_URL=https://devnet.cisco.com/v1/devenv-community/galileo/sessions` (or the corresponding `devnet-testing.cisco.com` URL). This mode uses existing `DEVENV_USER` and `DEVENV_PASSWORD` only on those trusted Cisco hosts. Previously configured DevNet session descriptors remain supported. `LLM_BASE_URL` alone does not select the DevNet gateway during new Workshop setup.

Preloaded `GALILEO_API_KEY` remains supported in every profile and takes precedence over temporary sessions, retaining the native Splunk Observability destination even when obsolete seed-URL variables are present. A native key with an explicit native `GALILEO_API_BASE_URL` can also read earlier saved runs from that destination. Saved runs remain bound to their original API destination. Anonymous credential fetching, including the former Pastebin default and all `GALILEO_*KEY_SEED_URL` sources, has been removed. A Workshop configuration containing only `GALILEO_WORKSHOP_KEY_SEED_URL` fails without fetching the URL or falling back to Connect. Stop on initialization failure. Do not store raw credentials in this repository.

For direct API workshops, provide one shared, project-scoped Editor key through the approved runtime secret mechanism; individual runtime keys are not required. Rotate or revoke it only after the whole cohort finishes. Use fresh learner pods without stale `.env` or `lab.env` settings: initialization retains its existing `.env` precedence. Viewer sign-in remains separate, and API key rotation does not delete saved results.

Enable code-free Connect for the workshop window before offering the exercises. Set `AUTO_CONNECT_ENABLED=true`, `OPEN_ADMISSION_ENABLED=true` and an explicit RFC 3339 `OPEN_ADMISSION_UNTIL` on the gateway. The gateway admits up to 100 active automatic grants by default; the operator can configure this with `OPEN_ADMISSION_MAX_ACTIVE`. Give each learner an individual organization Read-only identity with Viewer access to AI Observability Lab, and verify sign-in separately. A Viewer login permits UI exploration; it does not supply the runtime ingestion credential. The helper does not create identities or grants. Learners without viewer access can complete the API path once their API session is enabled.

Use an individual UI identity for each learner so they can keep their own signed-in session.

Before the workshop, check a fresh learner environment: the first play action should complete a baseline batch, and the learner should be able to open AI Observability Lab with their Viewer account.

The older profiles remain supported with their existing settings:

| Profile | Destination |
| --- | --- |
| `source 0-init-lab.sh` | Original `barry-2` destination |
| `source 0-init-lab.sh observability` | SplunkSE console `https://console.multitenant.galileocloud.io/splunkse`; **AI Observability Lab** project `59f3d09d-c104-4f40-a3b8-77af51ea55d1`; **ai-observability-lab** stream `3d95e2c0-38cd-4c1a-bdd8-7a259de8a33b` |

The original and SplunkSE profiles require a preloaded `GALILEO_API_KEY`; initialization fails clearly when it is missing. They no longer fetch credentials from seed URLs. The no-argument command still selects the original profile. Select the intended profile before running commands; the tenants do not share projects or credentials.

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
| `scorers` | List visible Splunk Observability scorers |
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

Refund experiments replay simulated tools without additional model calls. The helper calculates and uploads their application metrics. The gate reads the saved report and applies local thresholds. Splunk Observability scorers must be enabled separately.

The Agent Control credential-term regex allows the safe model call once and denies a matching request with zero protected calls. Both decisions are logged. Benign requests containing its terms can match and paraphrases can pass. This is separate from the email/phone check, not a general PII detector.

`llm-check` requires a live response. Original interactive and trace examples retain a labeled local fallback after model failures; that response is not recorded as a model span.

Human-review commands record sample ratings rather than independent judgments. A saved widget definition does not prove a chart has data. Check metric availability, span/trace aggregation, and the time range.

## Files

- `0-init-lab.sh` selects the destination and prepares the session.
- `barrybot.py` calls the model supplied by the lab.
- `galileo_client.py` provides the Splunk Observability REST client.
- `galileo_lab.py` provides the CLI commands.
- `support_agent.py` defines simulated refund paths, application metrics, and traces.
- `samples/eval_cases.csv` contains refund evaluation cases.
- `agent_control_demo.py` defines the local credential control.
- `data/galileo_api_capabilities.json` contains the API catalog.
