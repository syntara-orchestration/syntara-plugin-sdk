.PHONY: install test test-examples test-integration test-execution-plane-helpers test-execution-plane lint check generate-container-protocol

install:
	uv sync --locked --all-groups

test:
	uv run --no-sync pytest sdks/python/tests examples tests/execution_plane/test_helpers.py -q

test-examples:
	uv run --no-sync pytest examples -q

test-integration:
	SYNTARA_RUNTIME_IMAGE_TESTS=1 uv run --no-sync pytest -m integration -q

test-execution-plane-helpers:
	uv run --no-sync pytest tests/execution_plane/test_helpers.py -q
	uv run --no-sync ruff check tests/execution_plane
	for script in tests/execution_plane/*.sh; do bash -n "$$script" || exit; done

test-execution-plane:
	bash tests/execution_plane/run_compat.sh

lint:
	uv run --no-sync pre-commit run ruff --all-files

check:
	uv run --no-sync pre-commit run --all-files

generate-container-protocol:
	uv run --no-sync python -m grpc_tools.protoc \
		--proto_path=contracts/proto \
		--python_out=sdks/python/packages/contracts/src \
		--grpc_python_out=sdks/python/packages/contracts/src \
		contracts/proto/syntara_plugin/runtime/protocol/container.proto
