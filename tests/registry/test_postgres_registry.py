#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi>=0.110",
#     "httpx>=0.27",
#     "pyyaml>=6.0",
#     "jsonschema>=4.18",
#     "sqlmodel>=0.0.14",
#     "pytest>=8.0",
#     "psycopg[binary]>=3.1",
# ]
# ///
"""End-to-end registry + advertisement prototype for the ``http_request`` step.

    ┌───────────────────────────────────────────────────────────────────┐
    │ CONTROL-PLANE SDK DELIVERABLE — the registration & advertisement   │
    │ half of the Step SDK. Where test_register.py proves validate ->    │
    │ compile -> persist for a single row, this proves the *full* loop    │
    │ the visual builder depends on: a REST surface that ingests a        │
    │ manifest, persists the compiled descriptor to PostgreSQL, and       │
    │ advertises it back to the React Flow canvas.                        │
    └───────────────────────────────────────────────────────────────────┘

The prototype wires three layers together:

1. **Storage** - a ``StepType`` SQLModel table matching the target platform DDL,
   with ``descriptor`` stored as JSONB on PostgreSQL, falling back to
   generic JSON on SQLite so the same model runs in-memory in CI.
2. **SDK pipeline** - load ``manifest.yaml``, validate it against
   ``common-definitions.json`` (Draft-07, via ``$ref``), and compile it into the
   runtime ``step-definition.json`` descriptor.
3. **Advertisement API** - a FastAPI app implementing the registry REST contract
   documented in ``step-sdk-architecture.md`` (standard ``{data, meta}`` envelope,
   id-addressable resources, pagination + filtering) plus the two React Flow
   visual-builder additions that doc calls out:

     * ``POST /api/v1/step-types``                    - register (ingests a
       YAML/JSON manifest, validates, compiles, upserts). Returns ``{data: record}``.
     * ``GET  /api/v1/step-types``                    - list envelope; filter by
       ``category`` / ``enabled``; ``?view=palette`` returns
       the React Flow drawer summaries instead of the record projection.
     * ``GET  /api/v1/step-types/{id_or_name}``       - fetch one record; the path
       accepts a UUID *or* a step ``name`` (the UI-facing alias).
     * ``GET  /api/v1/step-types/{id_or_name}/descriptor`` - the raw compiled
       ``step-definition.json``, unwrapped, for direct canvas form rendering.

Run standalone (fetches deps in an ephemeral env):

    uv run schemas/examples/http-request/test_postgres_registry.py

Or under pytest once deps are installed:

    pytest schemas/examples/http-request/test_postgres_registry.py

Set ``SYNTARA_TEST_DATABASE_URL`` to a reachable PostgreSQL URL to exercise the
real JSONB column and DDL CHECK constraint instead of the SQLite default, e.g.

    SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/syntara_test
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import (
    Column,
    Field,
    Session,
    SQLModel,
    UniqueConstraint,
    create_engine,
    select,
)
from syntara_tools.compiler import (
    load_manifest as sdk_load_manifest,
)
from syntara_tools.compiler import (
    validate_manifest as sdk_validate_manifest,
)

REGISTRY_TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = REGISTRY_TESTS_DIR.parent.parent
SCHEMAS_ROOT = REPO_ROOT / "schemas"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "steps"
MANIFEST_PATH = FIXTURES_DIR / "http_request" / "manifest.yaml"
COMPILED_PATH = REGISTRY_TESTS_DIR / "step-definition.json"

# Point this at a reachable PostgreSQL to run the DB-configuration path against a
# real JSONB column + DDL CHECK instead of the in-memory SQLite default.
POSTGRES_URL_ENV = "SYNTARA_TEST_DATABASE_URL"

# React Flow palette fallback glyphs, keyed by step category. A manifest may
# override this per step via a top-level ``icon`` field (see the StepPaletteIcon
# definition in common-definitions.json).
ICON_BY_CATEGORY = {
    "action": "cube",
    "task": "terminal",
    "workflow": "project-diagram",
    "trigger": "bolt",
}

# List pagination default, matching the documented registry API meta envelope.
DEFAULT_PAGE_LIMIT = 50


# ---------------------------------------------------------------------------
# Registry model - matches the target platform step_types DDL.
# ---------------------------------------------------------------------------
class StepCategory(StrEnum):
    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class RegistryModel(SQLModel):
    """Base with a private MetaData so this prototype's ``step_types`` table does
    not collide with the identically named table in the sibling
    ``test_register.py`` when both are collected in a single pytest session."""

    metadata = MetaData()


class StepType(RegistryModel, table=True):
    """Isolated legacy registry-prototype record.

    The ``descriptor`` column stores the full compiled ``step-definition.json``
    so the canvas can render input forms without a frontend deployment.
    ``(name, version)`` is an internal prototype storage key, not SDK identity.
    """

    __tablename__ = "step_types"
    __table_args__ = (
        UniqueConstraint(
            "namespace", "name", "version", name="uq_step_types_namespace_name_version"
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    namespace: str = Field(index=True)
    name: str = Field(index=True)
    display_name: str
    version: str = Field(default="1.0.0")
    # Persist the enum *value* ("action"/"container") as TEXT + CHECK IN (...),
    # matching the target DDL. native_enum=False keeps this portable across
    # SQLite and PostgreSQL and stores the value, not the member name.
    category: StepCategory = Field(
        sa_column=Column(
            SAEnum(
                StepCategory,
                native_enum=False,
                values_callable=lambda e: [m.value for m in e],
            ),
            index=True,
            nullable=False,
        )
    )
    image_ref: str | None = Field(default=None)
    descriptor: dict[str, Any] = Field(
        sa_column=Column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    )
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# SDK pipeline: load -> validate -> compile.
# ---------------------------------------------------------------------------

def load_manifest() -> dict[str, Any]:
    """Load the test manifest (http-request)."""
    return sdk_load_manifest(MANIFEST_PATH)


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Validate manifest against platform schema."""
    return sdk_validate_manifest(manifest, schemas_root=SCHEMAS_ROOT)


def compile_manifest(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile manifest.yaml -> step-definition.json (the persisted artifact).

    When no manifest is provided the on-disk ``manifest.yaml`` is loaded. The
    compiled descriptor is written back to ``step-definition.json`` so the build
    artifact stays in sync.

    The K8s CRD structure (apiVersion, kind, metadata, spec) is preserved in the
    compiled output and stored directly in PostgreSQL JSONB.
    """
    descriptor = manifest if manifest is not None else load_manifest()
    with COMPILED_PATH.open("w") as handle:
        json.dump(descriptor, handle, indent=2)
    return descriptor


# ---------------------------------------------------------------------------
# Advertisement helpers: descriptor / row -> API representations.
# ---------------------------------------------------------------------------
def _version_key(version: str) -> tuple[int, ...]:
    """Coarse numeric sort key for the isolated legacy storage model."""
    return tuple(int(n) for n in re.findall(r"\d+", version)) or (0,)


def _latest(rows: list[StepType]) -> StepType:
    return max(rows, key=lambda r: _version_key(r.version))


def _latest_per_name(rows: list[StepType]) -> list[StepType]:
    """Collapse legacy storage rows to their highest test-only version."""
    by_name: dict[str, StepType] = {}
    for row in rows:
        current = by_name.get(row.name)
        if current is None or _version_key(row.version) > _version_key(current.version):
            by_name[row.name] = row
    return sorted(by_name.values(), key=lambda r: r.display_name)


def _summarize_whitespace(text: str) -> str:
    """Collapse a multi-line manifest description into a single palette line."""
    return re.sub(r"\s+", " ", text).strip()


def _palette_summary(row: StepType) -> dict[str, Any]:
    """Project a row into the React Flow drag-and-drop drawer summary (camelCase)."""
    descriptor = row.descriptor
    metadata = descriptor.get("metadata", {})
    icon = metadata.get("icon") or ICON_BY_CATEGORY.get(str(row.category), "cube")
    return {
        "name": row.name,
        "namespace": row.namespace,
        "displayName": row.display_name,
        "category": str(row.category),
        "version": row.version,
        "description": _summarize_whitespace(metadata.get("description", "")),
        "icon": icon,
    }


def _list_item(row: StepType) -> dict[str, Any]:
    """The compact list projection from the documented registry API."""
    return {
        "id": str(row.id),
        "name": row.name,
        "namespace": row.namespace,
        "category": str(row.category),
        "enabled": row.enabled,
    }


def _record(row: StepType) -> dict[str, Any]:
    """The full single-resource record (snake_case) from the documented API."""
    return {
        "id": str(row.id),
        "name": row.name,
        "namespace": row.namespace,
        "display_name": row.display_name,
        "category": str(row.category),
        "image_ref": row.image_ref,
        "version": row.version,
        "enabled": row.enabled,
        "descriptor": row.descriptor,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# FastAPI application factory.
# ---------------------------------------------------------------------------
def create_app(engine) -> FastAPI:
    """Build the advertisement API bound to a specific engine.

    The session dependency is bound to the provided engine so tests can inject a
    fresh in-memory database while production wires in a real PostgreSQL engine.
    """
    app = FastAPI(title="Syntara Step Registry", version="1.0.0")

    def get_session():
        with Session(engine) as session:
            yield session

    def _resolve(session: Session, id_or_name: str) -> StepType:
        """Resolve a path segment that is either a UUID or a step name.

        Name lookups return the latest version — the UI-facing addressing mode.
        """
        try:
            step_id = UUID(id_or_name)
        except ValueError:
            rows = session.exec(
                select(StepType).where(StepType.name == id_or_name)
            ).all()
            if not rows:
                raise HTTPException(404, f"step type '{id_or_name}' is not registered")
            return _latest(list(rows))
        row = session.get(StepType, step_id)
        if row is None:
            raise HTTPException(404, f"step type '{id_or_name}' is not registered")
        return row

    @app.post("/api/v1/step-types", status_code=201)
    async def register_step_type(
        request: Request,
        image_ref: str | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Validate, compile, and upsert a step manifest into the registry.

        The raw request body is read directly and parsed as YAML (a superset of
        JSON), so the endpoint accepts either format regardless of the declared
        ``Content-Type`` — a plain ``--data-binary @manifest.yaml`` just works.
        """
        raw = await request.body()
        if not raw.strip():
            raise HTTPException(
                400, "request body is empty; POST a YAML or JSON step manifest"
            )
        try:
            manifest = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise HTTPException(400, f"manifest is not valid YAML/JSON: {exc}")
        if not isinstance(manifest, dict):
            raise HTTPException(400, "manifest must be a mapping")

        errors = validate_manifest(manifest)
        if errors:
            raise HTTPException(
                422, {"message": "manifest failed schema validation", "errors": errors}
            )

        descriptor = compile_manifest(manifest)
        # Extract fields from K8s CRD structure
        metadata = descriptor["metadata"]
        spec = descriptor["spec"]

        name = metadata["name"]
        # This isolated registry prototype predates plugin registration. The
        # parent plugin would supply this namespace in the real discovery path.
        namespace = "syntara"
        # Plugin registration supplies this value; steps have no independent version.
        version = "1.0.0"
        # Where the plugin artifact lives. Distinct from spec.execution.image,
        # which names the runtime the step executes in.

        # Upsert on (name, version): re-publishing the same version updates in
        # place, a new version inserts a new row.
        existing = session.exec(
            select(StepType)
            .where(StepType.namespace == namespace)
            .where(StepType.name == name)
            .where(StepType.version == version)
        ).one_or_none()
        row = existing or StepType(namespace=namespace, name=name, version=version)
        if existing is None:
            session.add(row)

        row.display_name = metadata.get("displayName", name)
        row.category = StepCategory(spec["category"])
        row.image_ref = image_ref
        row.descriptor = descriptor
        row.enabled = True
        row.updated_at = datetime.utcnow()

        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, f"registration violates a constraint: {exc.orig}")
        session.refresh(row)
        return {"data": _record(row)}

    @app.get("/api/v1/step-types")
    def list_step_types(
        category: str | None = None,
        enabled: bool | None = None,
        view: str | None = Query(
            None, description="Set to 'palette' for React Flow drawer summaries."
        ),
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """List step types with the documented ``{data, meta}`` envelope."""
        stmt = select(StepType)
        try:
            if category is not None:
                stmt = stmt.where(StepType.category == StepCategory(category))
        except ValueError as exc:
            raise HTTPException(400, f"invalid filter value: {exc}")
        if enabled is not None:
            stmt = stmt.where(StepType.enabled == enabled)

        rows = list(
            session.exec(stmt.order_by(StepType.name, StepType.version)).all()
        )

        if view == "palette":
            # Drawer view: one entry per legacy row, UI camelCase shape.
            summaries = [_palette_summary(row) for row in _latest_per_name(rows)]
            return {
                "data": summaries,
                "meta": {"total": len(summaries), "limit": limit, "offset": offset},
            }

        page = rows[offset : offset + limit]
        return {
            "data": [_list_item(row) for row in page],
            "meta": {"total": len(rows), "limit": limit, "offset": offset},
        }

    @app.get("/api/v1/step-types/{id_or_name}")
    def get_step_type(
        id_or_name: str,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Fetch a single step type record (id- or name-addressable)."""
        return {"data": _record(_resolve(session, id_or_name))}

    @app.get("/api/v1/step-types/{id_or_name}/descriptor")
    def get_step_descriptor(
        id_or_name: str,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Return the raw compiled descriptor, unwrapped, for the canvas renderer.

        This is the one deliberate exception to the standard envelope: the React
        Flow canvas consumes ``step-definition.json`` verbatim to render forms,
        default values, field groupings, and port bindings.
        """
        return _resolve(session, id_or_name).descriptor

    return app


def make_engine(url: str | None = None):
    """Create an engine + schema. In-memory SQLite by default; Postgres if given."""
    if url:
        engine = create_engine(url)
    else:
        # A single shared in-memory connection so the schema persists across the
        # separate sessions each TestClient request opens.
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    RegistryModel.metadata.drop_all(engine)
    RegistryModel.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def engine():
    eng = make_engine(os.environ.get(POSTGRES_URL_ENV) or None)
    try:
        yield eng
    finally:
        RegistryModel.metadata.drop_all(eng)


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app(engine))


def _register_manifest(client: TestClient):
    """POST the on-disk http-request manifest as raw YAML."""
    return client.post(
        "/api/v1/step-types",
        content=MANIFEST_PATH.read_text(),
        headers={"Content-Type": "application/yaml"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_register_http_request_succeeds(client: TestClient) -> None:
    """Test 1: POST /step-types successfully registers the http-request step."""
    response = _register_manifest(client)
    assert response.status_code == 201, response.text
    record = response.json()["data"]
    assert record["name"] == "http_request"
    assert record["enabled"] is True
    # Registering a bare manifest supplies no artifact ref, and the runtime
    # image must not be borrowed to fill it.
    assert record["image_ref"] is None
    runtime = record["descriptor"]["spec"]["execution"]["image"]
    assert runtime == "quay.io/syntara/http-request-executor:latest"
    UUID(record["id"])  # id is a real UUID


def test_palette_summary_matches_ui_contract(client: TestClient) -> None:
    """Test 2: GET /step-types?view=palette returns the React Flow drawer summary."""
    assert _register_manifest(client).status_code == 201

    response = client.get("/api/v1/step-types", params={"view": "palette"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"data", "meta"}
    assert payload["meta"] == {"total": 1, "limit": 50, "offset": 0}

    summaries = payload["data"]
    assert isinstance(summaries, list) and len(summaries) == 1
    summary = summaries[0]
    assert set(summary) == {
        "name",
        "namespace",
        "displayName",
        "category",
        "version",
        "description",
        "icon",
    }
    assert summary["name"] == "http_request"
    assert summary["displayName"] == "HTTP Request"
    assert summary["category"] == "action"
    assert summary["version"] == "1.0.0"
    assert summary["icon"] == "globe"
    # Description is collapsed to a single palette-friendly line.
    assert "\n" not in summary["description"]
    assert summary["description"].startswith("Non-blocking HTTP/HTTPS API orchestrator")


def test_list_envelope_matches_documented_contract(client: TestClient) -> None:
    """The default list view uses the documented {data, meta} + filters contract."""
    assert _register_manifest(client).status_code == 201

    response = client.get(
        "/api/v1/step-types", params={"category": "action", "enabled": "true"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["meta"] == {"total": 1, "limit": 50, "offset": 0}
    item = payload["data"][0]
    assert set(item) == {"id", "name", "namespace", "category", "enabled"}
    assert item["category"] == "action"

    # A non-matching filter yields an empty page, not an error.
    empty = client.get("/api/v1/step-types", params={"category": "trigger"}).json()
    assert empty["data"] == [] and empty["meta"]["total"] == 0


def test_fetch_record_by_id_and_by_name(client: TestClient) -> None:
    """A record is addressable by UUID and by its step name (the UI alias)."""
    record = _register_manifest(client).json()["data"]
    step_id = record["id"]

    by_id = client.get(f"/api/v1/step-types/{step_id}")
    by_name = client.get("/api/v1/step-types/http_request")
    assert by_id.status_code == by_name.status_code == 200
    assert by_id.json()["data"]["id"] == by_name.json()["data"]["id"] == step_id
    assert "descriptor" in by_id.json()["data"]


def test_canvas_descriptor_exposes_inputs_and_output_envelope(
    client: TestClient,
) -> None:
    """Test 3: GET /step-types/{name}/descriptor returns the raw descriptor."""
    assert _register_manifest(client).status_code == 201

    response = client.get("/api/v1/step-types/http_request/descriptor")
    assert response.status_code == 200, response.text
    descriptor = response.json()  # raw step-definition.json, unwrapped

    # Verify K8s CRD structure
    assert descriptor["apiVersion"] == "syntara.io/v1alpha1"
    assert descriptor["kind"] == "StepType"
    assert "metadata" in descriptor
    assert "spec" in descriptor

    # Draft-07 inputs schema is present for dynamic form rendering (in spec section).
    spec = descriptor["spec"]
    inputs = spec["inputs"]
    assert "url" in inputs["properties"]
    assert inputs["properties"]["url"]["type"] == "string"
    assert inputs["required"] == ["url", "method"]

    # Output envelope references the immutable StandardOutputWrapper.
    outputs = spec["outputs"]
    assert outputs["allOf"][0]["$ref"].endswith("StandardOutputWrapper")
    assert "Result" in outputs["properties"]


def test_unique_constraint_rejects_duplicate_name_version(engine) -> None:
    """Test 4: (name, version) is unique so versions coexist but duplicates do not."""
    descriptor = compile_manifest()
    with Session(engine) as session:
        for _ in range(2):
            session.add(
                StepType(
                    namespace="syntara",
                    name="http_request",
                    display_name="HTTP Request",
                    version="1.0.0",
                    category=StepCategory.ACTION,
                    image_ref="quay.io/syntara/http-request-executor:latest",
                    descriptor=descriptor,
                )
            )
        with pytest.raises(IntegrityError):
            session.commit()


def test_register_same_version_is_idempotent_upsert(client: TestClient) -> None:
    """Re-registering the same (name, version) updates in place, no duplicate."""
    assert _register_manifest(client).status_code == 201
    assert _register_manifest(client).status_code == 201
    payload = client.get("/api/v1/step-types").json()
    assert payload["meta"]["total"] == 1


def test_get_unknown_step_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/step-types/does_not_exist").status_code == 404
    assert (
        client.get("/api/v1/step-types/does_not_exist/descriptor").status_code == 404
    )


def test_register_rejects_invalid_manifest(client: TestClient) -> None:
    """A manifest missing required taxonomy fields is rejected at validation."""
    broken = {"stepType": "broken", "inputs": {"properties": {}}}
    response = client.post("/api/v1/step-types", json=broken)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Standalone runner (mirrors pytest, prints a readable summary)
# ---------------------------------------------------------------------------
def _run_standalone() -> int:
    def fresh_client() -> TestClient:
        return TestClient(
            create_app(make_engine(os.environ.get(POSTGRES_URL_ENV) or None))
        )

    def fresh_engine():
        return make_engine(os.environ.get(POSTGRES_URL_ENV) or None)

    cases: list[tuple[str, Any]] = [
        ("test_register_http_request_succeeds", lambda: test_register_http_request_succeeds(fresh_client())),
        ("test_palette_summary_matches_ui_contract", lambda: test_palette_summary_matches_ui_contract(fresh_client())),
        ("test_list_envelope_matches_documented_contract", lambda: test_list_envelope_matches_documented_contract(fresh_client())),
        ("test_fetch_record_by_id_and_by_name", lambda: test_fetch_record_by_id_and_by_name(fresh_client())),
        ("test_canvas_descriptor_exposes_inputs_and_output_envelope", lambda: test_canvas_descriptor_exposes_inputs_and_output_envelope(fresh_client())),
        ("test_unique_constraint_rejects_duplicate_name_version", lambda: test_unique_constraint_rejects_duplicate_name_version(fresh_engine())),
        ("test_register_same_version_is_idempotent_upsert", lambda: test_register_same_version_is_idempotent_upsert(fresh_client())),
        ("test_get_unknown_step_returns_404", lambda: test_get_unknown_step_returns_404(fresh_client())),
        ("test_register_rejects_invalid_manifest", lambda: test_register_rejects_invalid_manifest(fresh_client())),
    ]
    passed = skipped = failed = 0
    for name, run in cases:
        try:
            run()
        except pytest.skip.Exception as exc:  # type: ignore[attr-defined]
            skipped += 1
            print(f"  - {name}: SKIP ({exc})")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ✓ {name}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
