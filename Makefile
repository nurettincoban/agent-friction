.PHONY: install check lint format typecheck test cov examples demo

install:
	uv sync --group dev

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

cov:
	uv run pytest --cov --cov-report=term-missing

examples:
	uv run python scripts/build_examples.py

demo:
	uv run agent-friction demo -o demo.html
