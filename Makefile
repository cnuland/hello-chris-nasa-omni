# NASA Omni inventory POC: top-level targets (docs/spec/00-conventions.md, docs/tdd-log/integration.md).
#
#   make test   every component suite (inventory-api, perception, agent, eval incl. its gates, dashboard) + tests/e2e
#   make eval   full pipeline eval: synthetic scene -> perception replay -> fresh test_int_* DB -> report -> strict gate
#   make lint   ruff on every Python component and tests/e2e, tsc --noEmit on the dashboard
#   make venvs  create any missing component venv (uv, Python 3.11) and dashboard node_modules
#
# Needs the local test Postgres (pgvector/pgvector:pg16) at TEST_PG_ADMIN_URL; the default is the throwaway test
# container documented in docs/spec/00-conventions.md. Suites only create/drop databases with their own test_*
# prefix; nothing here touches the "inventory" database.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
NPM ?= npm
TEST_PG_ADMIN_URL ?= postgresql://postgres:test@localhost:55432/postgres
export TEST_PG_ADMIN_URL
SEED ?= 7
EVAL_DB_PREFIX ?= test_int_

API := services/inventory-api
PER := services/perception
AGT := services/agent
EVAL := eval
UI := dashboard
E2E := tests/e2e

PY_VENVS := $(API)/.venv $(PER)/.venv $(AGT)/.venv $(EVAL)/.venv $(E2E)/.venv

.PHONY: test test-api test-perception test-agent test-eval test-dashboard test-e2e eval lint venvs

# ------------------------------------------------------------------------------------------------ test
test: test-api test-perception test-agent test-eval test-dashboard test-e2e
	@echo "make test: all suites passed"

test-api: | $(API)/.venv
	cd $(API) && .venv/bin/python -m pytest -q -p no:cacheprovider

test-perception: | $(PER)/.venv
	cd $(PER) && .venv/bin/python -m pytest -q -p no:cacheprovider

test-agent: | $(AGT)/.venv
	cd $(AGT) && .venv/bin/python -m pytest -q -p no:cacheprovider

# eval's own `make test`: unit + integration suite (real perception replay), strict CI gate on the committed
# snapshot, and the gate over the newest local pipeline reports (AC-EVAL-6).
test-eval: | $(EVAL)/.venv
	$(MAKE) -C $(EVAL) test

test-dashboard: | $(UI)/node_modules
	cd $(UI) && $(NPM) test --silent

test-e2e: | $(E2E)/.venv
	cd $(E2E) && .venv/bin/python -m pytest -q -p no:cacheprovider

# ------------------------------------------------------------------------------------------------ eval
# Generated frames go to a temp dir (not the tree); the report goes to eval/reports (git-ignored).
eval: | $(EVAL)/.venv
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	cd $(EVAL) && \
	.venv/bin/python -m nasa_eval generate --seed $(SEED) --out "$$tmp" && \
	EVAL_PG_ADMIN_URL="$(TEST_PG_ADMIN_URL)" .venv/bin/python -m nasa_eval run --dataset "$$tmp/synth-$(SEED)" \
		--target local --db-prefix $(EVAL_DB_PREFIX) && \
	.venv/bin/python -m nasa_eval gate

# Real-video location eval (docs/spec/50-eval.md AC-EVAL-10): scores the live run of the NASA tour against its
# caption module timeline. REPORT-ONLY (needs the live cluster, never a CI gate); read-only GETs on EVAL_REAL_API,
# or EVAL_REAL_ARGS="--export export.json" (see `python -m nasa_eval.real.score_run --help`). Report: eval/reports/real.
EVAL_REAL_API ?= https://dashboard-nasa-app.apps.bgrr25lx.eastus.aroapp.io/api
EVAL_REAL_SRT ?= $(HOME)/nasa-media/iss-tour/tour.srt
EVAL_REAL_ARGS ?= --api $(EVAL_REAL_API)
.PHONY: eval-real
eval-real: | $(EVAL)/.venv
	cd $(EVAL) && .venv/bin/python -m nasa_eval.real.score_run $(EVAL_REAL_ARGS) --srt "$(EVAL_REAL_SRT)"

# ------------------------------------------------------------------------------------------------ lint
lint: | $(PY_VENVS) $(UI)/node_modules
	cd $(API) && .venv/bin/ruff check .
	cd $(PER) && .venv/bin/ruff check .
	cd $(AGT) && .venv/bin/ruff check .
	cd $(EVAL) && .venv/bin/ruff check .
	cd $(E2E) && .venv/bin/ruff check .
	cd $(UI) && $(NPM) run --silent typecheck

# ------------------------------------------------------------------------------------------------ environments
venvs: | $(PY_VENVS) $(UI)/node_modules

$(API)/.venv $(PER)/.venv $(AGT)/.venv:
	$(UV) venv --python 3.11 $@
	$(UV) pip install --python $@ -e '$(patsubst %/.venv,%,$@)[test]'

$(EVAL)/.venv:
	$(UV) venv --python 3.11 $@
	$(UV) pip install --python $@ -e '$(EVAL)[test,pipeline]'

$(E2E)/.venv:
	$(UV) venv --python 3.11 $@
	cd $(E2E) && $(UV) pip install --python .venv -r requirements.txt

$(UI)/node_modules:
	cd $(UI) && $(NPM) ci
