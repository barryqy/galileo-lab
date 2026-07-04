# Galileo DevNet Lab Helper

Student helper repo for the Galileo DevNet learning lab.

## What Is Here

- `0-init-lab.sh` checks local configuration and prepares a private `.galileo/` state folder
- `barrybot.py` builds BarryBot on the LLM proxy provided by the DevNet lab image
- `galileo_client.py` is a small REST client for Galileo API calls
- `galileo_lab.py` provides the lab commands used by the DevNet instructions
- `support_agent.py` defines the baseline and improved BarryBot agent paths
- `samples/eval_cases.csv` contains the release evaluation cases
- `scorers/credential_exfiltration.py` is the registered runtime protection metric
- `data/galileo_api_capabilities.json` summarizes the public Galileo API surface used in the lab

The hosted DevNet environment prepares the Galileo API session during `source 0-init-lab.sh`.

## Quick Start

```bash
cd /home/developer/src
git clone https://github.com/barryqy/galileo-lab.git
cd galileo-lab
python3 -m pip install -r requirements.txt --disable-pip-version-check
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
```
