# Step Registry API Tests

These tests verify a **legacy platform registry prototype** (`/api/v1/step-types`). It is test
support only, not an SDK persistence contract.

## What They Test

These are **platform-level tests**, not step-specific tests. They verify:

- **Manifest validation** - YAML schema validation against `schemas/common-definitions.json`
- **Compilation pipeline** - `manifest.yaml` → `step-definition.json`
- **Prototype storage** - JSONB column, DDL CHECK constraints, GIN indexing
- **REST API contract** - `{data, meta}` envelope, pagination, filtering (`GET /api/v1/step-types`)
- **Registration endpoint** - Publishing steps (`POST /api/v1/step-types`)
- **Canvas integration** - `?view=palette` and `/descriptor` endpoints for UI
- **Legacy upsert behavior** - Prototype-only storage behavior, not plugin version semantics
- **Name-addressable lookups** - Fetching by UUID or step name
- **DDL constraints** - Isolated prototype constraints; the SDK does not define storage keys

## Test Files

- **`test_postgres_registry.py`** (770 lines) - Complete registry prototype with 9 test cases
- **`test_register.py`** - Registration validation tests  
- **`test_integration.py`** - End-to-end integration tests

## Example Step Used

These tests use **`tests/fixtures/steps/http_request/manifest.yaml`** as an example step, but they're testing the **registry/platform layer**, not the HTTP request step itself.

Any valid step manifest could be used - the tests verify the prototype can:
1. Validates the manifest against the schema
2. Compiles it to a descriptor
3. Stores it in its isolated test database
4. Advertises it via REST API

## Run Tests

```bash
# Run from the repository root
uv run pytest tests/registry/test_postgres_registry.py

# Expected output:
# ✓ test_register_http_request_succeeds
# ✓ test_palette_summary_matches_ui_contract
# ✓ test_list_envelope_matches_documented_contract
# ✓ test_fetch_record_by_id_and_by_name
# ✓ test_canvas_descriptor_exposes_inputs_and_output_envelope
# ✓ test_check_constraint_rejects_container_without_image_ref
# ✓ test_register_same_version_is_idempotent_upsert
# ✓ test_get_unknown_step_returns_404
# ✓ test_register_rejects_invalid_manifest
#
# 9 passed, 0 skipped, 0 failed
```

## PostgreSQL Testing

By default, tests use SQLite in-memory. To test against real PostgreSQL:

```bash
export SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/syntara_test
uv run pytest tests/registry/test_postgres_registry.py
```

This tests the real JSONB column type and DDL constraints.

## See Also

- [../../schemas/common-definitions.json](../../schemas/common-definitions.json) - Platform meta-schema
- [../../docs/architecture.md](../../docs/architecture.md) - Registry architecture documentation
