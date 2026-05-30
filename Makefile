.venv:
	uv sync

setup: .venv

sync: .venv
	git checkout main
	git pull origin main
	uv run sync.py

.PHONY: setup sync
