.PHONY: typecheck test lint format install clean check help
.DEFAULT_GOAL := help

typecheck:
	poetry run mypy turbodl --exclude '__pycache__'

test:
	poetry run pytest

lint:
	poetry run ruff check turbodl

format:
	poetry run ruff format turbodl

install:
	poetry lock
	poetry install

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

check: typecheck lint test

help:
	@echo "Available commands:"
	@echo "  typecheck  - Run type checking with mypy"
	@echo "  test       - Run tests with pytest"
	@echo "  lint       - Check code with ruff"
	@echo "  format     - Format code with ruff"
	@echo "  install    - Update poetry.lock and install dependencies"
	@echo "  clean      - Remove cache and temporary files"
	@echo "  check      - Run typecheck, lint and test"
