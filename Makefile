PYTHON ?= python
CONFIG  = samples/aetherbound.locqa.yml
SHEET   = samples/aetherbound_ui.csv
SCRIPT  = samples/aetherbound.rpy
TRANS   = samples/aetherbound_ja.rpy

# Japanese in the reports outlives a Windows console's default codepage only
# if this is set; without it `make check` dies partway through a finding.
export PYTHONIOENCODING = utf-8

.PHONY: help install test eval sweep demo check graph pseudo lint clean

help:
	@echo "install   pip install -e . (editable, with PyYAML)"
	@echo "test      run the unit suite"
	@echo "eval      classifier evaluation against eval/labels.csv"
	@echo "sweep     voice.register threshold sweep"
	@echo "demo      regenerate demo-ui.html and demo-dialogue.html"
	@echo "check     run the checks over the sample sheet, to the console"
	@echo "graph     print the sample script's dialogue graph"
	@echo "clean     remove generated reports and caches"

install:
	$(PYTHON) -m pip install -e .

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
	rm -f demo-*.html loc-report.html loc-results.xml pseudo.csv sweep.png
	rm -rf .ruff_cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
