UV ?= uv
KILIX_STATE_DIR := third_party/kilix-state

.DEFAULT_GOAL := help

.PHONY: help setup setup-all test smoke-ml lint typecheck check doctor clean

help:
	@printf '%s\n' \
		'kilix-playalong' \
		'' \
		'  make setup      Build shared state library and sync the base uv environment' \
		'  make setup-all  Also install separation, transcription, and tab ML providers' \
		'  make check      Run tests, lint, and static typing' \
		'  make smoke-ml   Run the generated-tone Basic Pitch ONNX integration test' \
		'  make doctor     Inspect runtime/provider availability'

$(KILIX_STATE_DIR)/build/libkilix-state.so:
	$(MAKE) -C $(KILIX_STATE_DIR)

setup: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) sync --frozen --dev

setup-all: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) sync --frozen --dev --all-extras

test: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen pytest -m 'not ml'

smoke-ml: setup-all
	$(UV) run --frozen pytest -m ml

lint:
	$(UV) run --frozen ruff check src tests

typecheck:
	$(UV) run --frozen mypy

check: test lint typecheck

doctor: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen kilix-playalong doctor

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov
