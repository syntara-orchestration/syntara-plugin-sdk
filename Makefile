.PHONY: install test test-examples test-integration lint check generate-container-protocol

install:
	uv sync --locked --all-groups

test:
	uv run --no-sync pytest sdks/python/tests examples -q

test-examples:
	uv run --no-sync pytest examples -q

test-integration:
	SYNTARA_RUNTIME_IMAGE_TESTS=1 uv run --no-sync pytest -m integration -q

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
