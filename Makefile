.venv:
	uv venv
	uv pip install -r requirements.txt

setup: .venv

sync: .venv
	.venv/bin/python sync.py

.PHONY: setup sync
