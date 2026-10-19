.PHONY: install test test-examples lint check generate-container-protocol runtime-image runtime-image-archive

CONTAINER_ENGINE ?= podman
RUNTIME_IMAGE ?= syntara-plugin-runtime
RUNTIME_IMAGE_TAG ?= dev
RUNTIME_IMAGE_PLATFORM ?= linux/amd64

install:
	uv sync --locked --all-groups

test:
	uv run --no-sync pytest sdks/python/tests examples -q

test-examples:
	uv run --no-sync pytest examples -q

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

runtime-image:
	$(CONTAINER_ENGINE) build \
		--format oci \
		--platform $(RUNTIME_IMAGE_PLATFORM) \
		--file containers/runtime/Containerfile \
		--tag $(RUNTIME_IMAGE):$(RUNTIME_IMAGE_TAG) \
		.

runtime-image-archive:
	$(MAKE) runtime-image
	$(CONTAINER_ENGINE) save \
		--output dist/$(RUNTIME_IMAGE)-$(RUNTIME_IMAGE_PLATFORM).tar \
		$(RUNTIME_IMAGE):$(RUNTIME_IMAGE_TAG)
