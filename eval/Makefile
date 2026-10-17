# nasa_eval (docs/spec/50-eval.md). `make test` runs the unit suite and the regression gates (AC-EVAL-6).
PY ?= .venv/bin/python
SEED ?= 7

# CI gate: scores a committed, deterministic pipeline snapshot against the committed gold dataset and gates the
# result strictly, so `make test` fails on a scorer / threshold / baseline regression on every checkout.
CI_DATASET ?= datasets/synth-ci
CI_SNAPSHOT ?= baselines/synth-ci.snapshot.json
CI_REPORTS ?= reports/ci

.PHONY: test unit gate-ci gate-local lint gate eval venv venv-pipeline

venv:
	uv venv --python 3.11 .venv && uv pip install --python .venv -e '.[test]'

# Adds services/perception and services/inventory-api so `run` can replay datasets (Integration phase).
venv-pipeline: venv
	uv pip install --python .venv -e '.[pipeline]'

test: unit gate-ci gate-local

unit:
	$(PY) -m pytest -q

gate-ci:
	$(PY) -m nasa_eval score --dataset $(CI_DATASET) --snapshot $(CI_SNAPSHOT) --reports-dir $(CI_REPORTS)
	$(PY) -m nasa_eval gate --reports-dir $(CI_REPORTS)

# Newest local pipeline report of every dataset (from `make eval` / `nasa_eval run`); none yet is not an error here
# because gate-ci above already gates committed data.
gate-local:
	$(PY) -m nasa_eval gate --allow-missing-report

lint:
	$(PY) -m ruff check .

gate:
	$(PY) -m nasa_eval gate

# Full pipeline eval on a synthetic scene (needs `make venv-pipeline` + EVAL_PG_ADMIN_URL), then a strict gate.
eval:
	$(PY) -m nasa_eval generate --seed $(SEED)
	$(PY) -m nasa_eval run --dataset synth-$(SEED) --target local
	$(PY) -m nasa_eval gate
