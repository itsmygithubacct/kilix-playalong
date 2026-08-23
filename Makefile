UV ?= uv
KILIX_STATE_DIR := third_party/kilix-state

.DEFAULT_GOAL := help

# `uv sync` uses --locked so a lock that no longer matches pyproject.toml fails
# loudly instead of installing stale pins; `uv run` uses --frozen so no target
# can rewrite the committed uv.lock. `check` gates the lock itself, first.
#
# Every uv-invoking target takes the shared-library target as a prerequisite.
# None of them needs the library itself: they need the uv environment, and that
# cannot resolve until the kilix-state submodule is checked out, because
# pyproject.toml declares kilix-state-py as a path source inside it. Without the
# prerequisite, a clone made without --recurse-submodules fails with uv's
# "Distribution not found at: ..." instead of make's submodule build error.
# `check` is the one exemption: it runs no uv itself, only sub-makes that do.
# tests/test_build_gate.py asserts both properties.

.PHONY: help setup setup-all test smoke-ml lint format-check lock-check typecheck check check-all doctor clean \
	native native-test native-sanitize native-clean native-install native-deps

help:
	@printf '%s\n' \
		'kilix-playalong' \
		'' \
		'  make setup      Build shared state library and sync the base uv environment' \
		'  make setup-all  Also install separation, transcription, and tab ML providers' \
		'  make check      Run the lock, test, lint, format, and typing gates' \
		'  make smoke-ml   Run the Basic Pitch ONNX worker against a generated tone' \
		'  make doctor     Inspect runtime/provider availability' \
		'' \
		'  make native          Build the native Kilix surface (C11)' \
		'  make native-test     Run the native suites' \
		'  make native-sanitize Run them under ASan and UBSan' \
		'  make check-all       The Python gate and the native suites' \
		'' \
		'  smoke-ml invokes the worker module directly under the ambient environment,' \
		'  so it does not cover the hardened run_command provider boundary.'

$(KILIX_STATE_DIR)/build/libkilix-state.so:
	$(MAKE) -C $(KILIX_STATE_DIR)

setup: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) sync --locked --dev

setup-all: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) sync --locked --dev --all-extras

test: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen pytest -m 'not ml'

smoke-ml: setup-all
	$(UV) run --frozen pytest -m ml

lint: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen ruff check src tests

format-check: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen ruff format --check src tests

lock-check: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) lock --check

typecheck: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen mypy

# The lock gate is ordered by recipe lines, not by a prerequisite list:
# sibling prerequisites are unordered, so `check: lock-check test lint ...`
# runs the whole suite against a stale environment under `make -j` (common via
# MAKEFLAGS) while the lock gate is still deciding to fail. The remaining four
# gates are independent of one another and are deliberately left free to run in
# parallel: concurrent `uv run --frozen` invocations share one locked
# environment and do not write to it.
check:
	$(MAKE) lock-check
	$(MAKE) test lint format-check typecheck

# The native surface is a separate toolchain with a separate gate: C11, the
# vendored terminal stack and SDL2/libsndfile, none of which the Python gate
# can see. Delegating keeps one build graph per language rather than one
# Makefile that half-understands both, and keeps `make check` meaning exactly
# what it meant before the native surface existed.
native native-test native-sanitize native-clean native-install native-deps:
	$(MAKE) -f Makefile.native $@

# Both gates. Ordered, not parallel: they compete for the same cores and a
# sanitizer run that loses its cores to pytest reports timings nobody can use.
check-all:
	$(MAKE) check
	$(MAKE) native-test

doctor: $(KILIX_STATE_DIR)/build/libkilix-state.so
	$(UV) run --frozen kilix-playalong doctor

clean:
	rm -rf build dist .pytest_cache .ruff_cache .mypy_cache htmlcov
