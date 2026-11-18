PYTHON ?= python
CONFIG  = samples/aetherbound.locqa.yml
SHEET   = samples/aetherbound_ui.csv
SCRIPT  = samples/aetherbound.rpy
TRANS   = samples/aetherbound_ja.rpy

# Japanese in the reports survives a Windows pipe's default codepage only if
# this is set; without it the CLI substitutes ? for every unencodable character.
export PYTHONIOENCODING = utf-8

.PHONY: help install install-web test eval sweep demo check graph pseudo web worker lint clean

help:
	@echo "install   pip install -e . (editable, with PyYAML)"
	@echo "install-web  pip install -e .[web] (FastAPI, SQLAlchemy, uvicorn)"
	@echo "test      run the unit suite (web tests skip without the web extra)"
	@echo "web       serve the front end on :8000, worker as a thread inside it"
	@echo "worker    run a standalone worker (pair with LOCQA_INLINE_WORKER=0 make web)"
	@echo "eval      classifier evaluation against eval/labels.csv"
	@echo "sweep     voice.register threshold sweep"
	@echo "demo      regenerate demo-ui.html and demo-dialogue.html"
	@echo "check     run the checks over the sample sheet, to the console"
	@echo "graph     print the sample script's dialogue graph"
	@echo "clean     remove generated reports and caches"

install:
	$(PYTHON) -m pip install -e .

install-web:
	$(PYTHON) -m pip install -e ".[web]"

web:
	$(PYTHON) -m uvicorn locqa_web.app:app --reload --port 8000

worker:
	$(PYTHON) -m locqa_web.worker

test:
	$(PYTHON) -m unittest discover -s tests -v

eval:
	$(PYTHON) eval/evaluate.py --errors

sweep:
	$(PYTHON) eval/sweep.py

# The two proof sheets in the repo root. Ignored by git — they are output, and
# a generated file in version control is a file that goes stale silently.
demo:
	$(PYTHON) -m locqa.cli check $(SHEET) -c $(CONFIG) \
		-r html -o demo-ui.html --fail-on none
	$(PYTHON) -m locqa.cli check $(SCRIPT) -t $(TRANS) -c $(CONFIG) \
		-r html -o demo-dialogue.html --fail-on none
	@echo "wrote demo-ui.html and demo-dialogue.html"

check:
	$(PYTHON) -m locqa.cli check $(SHEET) -c $(CONFIG) --fail-on none

graph:
	$(PYTHON) -m locqa.cli graph $(SCRIPT) -t $(TRANS)

pseudo:
	$(PYTHON) -m locqa.cli pseudo $(SHEET) -o pseudo.csv
	@echo "wrote pseudo.csv"

lint:
	ruff check locqa tests eval

clean:
	rm -f demo-*.html loc-report.html loc-results.xml pseudo.csv sweep.png locqa_web.db*
	rm -rf .ruff_cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
