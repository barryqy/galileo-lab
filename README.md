# Galileo DevNet Lab Helper

Student helper repo for the Galileo DevNet learning lab.

## What Is Here

- `0-init-lab.sh` checks local configuration and prepares a private `.galileo/` state folder
- `barrybot.py` builds BarryBot on the LLM proxy provided by the DevNet lab image
- `galileo_client.py` is a small REST client for Galileo API calls
- `galileo_lab.py` provides the lab commands used by the DevNet instructions
- `samples/eval_cases.csv` is a small prompt evaluation dataset
- `data/galileo_api_capabilities.json` summarizes the public Galileo API surface used in the lab

The hosted DevNet lab retrieves the Galileo API key from the lab key-service during `source 0-init-lab.sh`; learners should not paste API keys into the repo or terminal.

## Quick Start

```bash
cd /home/developer/src
git clone https://github.com/barryqy/galileo-lab.git
cd galileo-lab
python3 -m pip install -r requirements.txt --disable-pip-version-check
source 0-init-lab.sh
python3 galileo_lab.py llm-check
python3 galileo_lab.py setup
python3 galileo_lab.py barrybot --ask "What should I watch first in Galileo?"
python3 galileo_lab.py log-traces
python3 galileo_lab.py query-traces
```
