# Galileo DevNet Lab Helper

Student helper repo for the Galileo DevNet learning lab.

## What Is Here

- `0-init-lab.sh` checks local configuration and prepares a private `.galileo/` state folder
- `barrybot.py` builds BarryBot on the LLM proxy provided by the DevNet lab image
- `galileo_client.py` is a small REST client for Galileo API calls
- `galileo_lab.py` provides the lab commands used by the DevNet instructions
- `support_agent.py` defines simulated baseline and candidate tool paths, application metrics, and traces with live LLM summaries
- `samples/eval_cases.csv` contains the release evaluation cases
- `agent_control_demo.py` defines the local Agent Control policy and checks it before the protected model function executes
- `data/galileo_api_capabilities.json` summarizes the public Galileo API surface used in the lab

The hosted DevNet environment prepares the Galileo API session during `source 0-init-lab.sh`.

## Quick Start

```bash
cd /home/developer/src
git clone https://github.com/barryqy/galileo-lab.git
cd galileo-lab
if [ -n "${VIRTUAL_ENV:-}" ] && type deactivate >/dev/null 2>&1; then deactivate; fi
python3 -m pip install uv --disable-pip-version-check
if [ ! -x .venv/bin/python ]; then python3 -m uv venv --python 3.12 .venv; fi
python3 -m uv pip install --python .venv/bin/python -r requirements.txt
source 0-init-lab.sh
python3 galileo_lab.py llm-check
python3 galileo_lab.py setup
python3 galileo_lab.py agent-demo
python3 galileo_lab.py dataset
python3 galileo_lab.py experiment
python3 galileo_lab.py release-gate
python3 galileo_lab.py guardrail
python3 galileo_lab.py human-workflows
python3 galileo_lab.py dashboard
python3 galileo_lab.py trends
```

`llm-check` requires a live response from the DevNet model. BarryBot's interactive and trace examples retain a local fallback when the model is unavailable; response metadata identifies that fallback and does not record it as a live LLM span.

The refund experiments replay simulated tools and upload their application metrics to Galileo. The runtime exercise uses the Agent Control SDK locally with a regex policy: the safe request reaches the live model once, while a matching credential request is denied with zero model calls. Both decisions are logged to Galileo. The pattern matches the listed terms; broader coverage requires additional evaluators and cases.
