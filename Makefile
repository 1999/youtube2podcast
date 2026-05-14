.venv:
	uv sync

setup: .venv

sync: .venv
	uv run sync.py

.PHONY: setup sync
