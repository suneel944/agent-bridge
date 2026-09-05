.PHONY: install install-dev install-system build check
install:
	@set -eu; \
	bridge_requirements=$$(mktemp); \
	trap 'rm -f "$$bridge_requirements"' EXIT; \
	uv export --locked --no-dev --no-emit-project --no-hashes > "$$bridge_requirements"; \
	uv tool install --python 3.12 . --with-requirements "$$bridge_requirements"

install-dev:
	@set -eu; \
	bridge_requirements=$$(mktemp); \
	trap 'rm -f "$$bridge_requirements"' EXIT; \
	uv export --locked --no-dev --no-emit-project --no-hashes > "$$bridge_requirements"; \
	uv tool install --python 3.12 --editable . --with-requirements "$$bridge_requirements"

install-system:
	@test "$$(id -u)" -eq 0 || { echo 'Run sudo env "PATH=$$PATH" make install-system'; exit 1; }
	UV_TOOL_DIR=/opt/agent-bridge/tools UV_TOOL_BIN_DIR=/usr/local/bin \
	UV_PYTHON_INSTALL_DIR=/opt/agent-bridge/python $(MAKE) install

build:
	uv build --no-sources

check:
	uv sync --locked
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	$(MAKE) build
	uv run --locked pytest -q
