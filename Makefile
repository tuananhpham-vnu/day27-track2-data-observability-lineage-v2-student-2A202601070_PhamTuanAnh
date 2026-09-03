PYTHON ?= python

.PHONY: reset baseline tests gx dbt dashboard generate evidence triage lineage drill all

reset:
	$(PYTHON) scripts/reset_lab.py

baseline:
	$(PYTHON) scripts/run_baseline.py

tests:
	pytest tests_public -q

gx:
	$(PYTHON) gx/validate_orders.py

dbt:
	$(PYTHON) scripts/sync_dbt_seeds.py
	dbt build --project-dir dbt_project --profiles-dir dbt_project

dashboard:
	streamlit run dashboard/app.py

generate:
	$(PYTHON) scripts/generate_data.py --rows 600 --days 42 --seed 27

# Side-by-side proof of what the starter baseline misses and the upgrade catches.
evidence:
	$(PYTHON) scripts/evidence.py

# Incident triage: localise what changed in data/incoming vs the baseline.
triage:
	$(PYTHON) scripts/triage.py --dataset orders

# OpenLineage events + reconciliation of the declared graph against dbt's manifest.
lineage:
	$(PYTHON) scripts/emit_lineage.py

# Blind drill: 8 fault classes absent from the public set, to prove the
# detection stack generalises beyond the faults it was built against.
drill:
	$(PYTHON) scripts/mystery_drill.py

# Full verification sweep from a clean state.
all: reset tests dbt baseline gx evidence lineage drill
