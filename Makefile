.PHONY: install check
install:
	@set -eu; \
	bridge_requirements=$$(mktemp); \
	trap 'rm -f "$$bridge_requirements"' EXIT; \
	uv export --locked --no-dev --no-emit-project --no-hashes > "$$bridge_requirements"; \
	uv tool install --python 3.12 --editable . --with-requirements "$$bridge_requirements"

check:
	uv sync --locked
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked pytest -q
