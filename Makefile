.PHONY: install test test-examples test-integration test-step-types-helpers test-step-types lint check generate-container-protocol

install:
	uv sync --locked --all-groups

test:
	uv run --no-sync pytest sdks/python/tests examples tests/step_types -q

test-examples:
	uv run --no-sync pytest examples -q

test-integration:
	SYNTARA_RUNTIME_IMAGE_TESTS=1 uv run --no-sync pytest -m integration -q

test-step-types-helpers:
	uv run --no-sync pytest tests/step_types -q
	uv run --no-sync ruff check tests/step_types
	bash -n tests/step_types/run_local.sh

test-step-types:
	bash tests/step_types/run_local.sh

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
