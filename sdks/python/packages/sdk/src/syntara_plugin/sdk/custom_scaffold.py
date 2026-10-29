"""Deterministic, single-Action custom-runner workspace scaffolding."""

from __future__ import annotations

import builtins
import json
import keyword
import math
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from .testgen import generate_step_tests

_BASE_IMAGE = (
    "registry.access.redhat.com/ubi9/python-312@sha256:"
    "e6a10c3150624fbfd33dbd7721ef99ce7a1001b417d64427ad58515ab8044121"
)


def write_custom_workspace(
    directory: Path,
    namespace: str,
    name: str,
    version: str,
    action: str,
    schema: dict[str, Any],
) -> None:
    """Write a self-contained workspace for one manifest-backed Action."""
    _validate_model_schema(schema)
    # Avoid shadowing the SDK's syntara_plugin namespace when name is "plugin".
    package = "syntara_step_" + name.replace("-", "_")
    target_path = f"steps/{action}/manifest.yaml"
    input_path = f"schemas/{action}.input.yaml"
    image = f"{name[:54].rstrip('-')}-workload"
    plugin = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "namespace": namespace,
            "name": name,
            "version": version,
            "displayName": _display_name(name),
            "description": "A Syntara plugin.",
        },
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "workload": {"image": image},
            "targets": [target_path],
        },
    }
    target = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": action,
            "displayName": _display_name(action),
            "description": "Example custom-runner action.",
        },
        "spec": {
            "runtime": {
                "kind": "custom-runner",
                "entryPoint": f"{package}.step:ActionImplementation",
            },
            "input": {"kind": "asset", "path": input_path},
            "output": {
                "kind": "inline",
                "value": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"resource": {"type": "string"}},
                    "required": ["resource"],
                },
            },
        },
    }
    files = {
        "plugin.yaml": _json(plugin),
        target_path: _json(target),
        input_path: _json(schema),
        "syntara-plugin.yaml": (
            "apiVersion: syntara.io/v1alpha1\nkind: PluginBuildSettings\nmanifest: plugin.yaml\n"
            f"build:\n  artifactOutput: dist/{name}.oci.tar\n  workload:\n"
            f"    engine: podman\n    context: .\n    containerfile: Containerfile\n"
            f"    imageId: {image}\n    tag: {version}\n"
        ),
        "runtime.json": _json(
            {
                "abi": "syntara.container/v1alpha1",
                "adapter": {"bindingsFactory": f"{package}.runtime:create_bindings"},
            }
        ),
        "Containerfile": _containerfile(),
        "pyproject.toml": _pyproject(name, version, package),
        f"{package}/__init__.py": '"""Custom-runner plugin package."""\n',
        f"{package}/step.py": _step(schema),
        f"{package}/runtime.py": _runtime(package, target_path, input_path),
        "docs/README.md": _readme(name, action),
    }
    for relative, contents in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    generate_step_tests(directory / "plugin.yaml")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n"


_ANNOTATIONS = {
    "title",
    "description",
    "examples",
    "default",
    "deprecated",
    "readOnly",
    "writeOnly",
}
_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength", "pattern", "enum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
    "null": set(),
}
_FIELD_CONSTRAINTS = {
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minItems": "min_length",
    "maxItems": "max_length",
}


def _validate_model_schema(schema: dict[str, Any]) -> None:
    """Reject schemas that cannot be represented safely before writing any files."""

    def finite(value: object, path: str) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"fields schema {path}: non-finite JSON numbers are not supported")
        if isinstance(value, float) and "e" in repr(value).lower():
            raise ValueError(f"fields schema {path}: scientific notation is not supported")
        if isinstance(value, dict):
            for key, child in value.items():
                finite(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                finite(child, f"{path}[{index}]")

    def visit(node: object, path: str) -> None:
        if not isinstance(node, dict):
            raise ValueError(f"fields schema {path}: expected a schema object")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _KEYWORDS:
            raise ValueError(f"fields schema {path}: unsupported or missing type {kind!r}")
        unsupported = sorted(set(node) - _ANNOTATIONS - _KEYWORDS[kind] - {"type"})
        if unsupported:
            raise ValueError(f"fields schema {path}: unsupported keyword {unsupported[0]!r}")
        if "default" in node:
            errors = list(
                Draft202012Validator(node, format_checker=FormatChecker()).iter_errors(
                    node["default"]
                )
            )
            if errors:
                raise ValueError(f"fields schema {path}: invalid default: {errors[0].message}")
            validate_default_types(node, node["default"], path)
        if "enum" in node:
            enum = node["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError(f"fields schema {path}: enum must contain at least one value")
            if not all(isinstance(value, str) for value in enum):
                raise ValueError(f"fields schema {path}: only string enums are supported")
        if kind == "integer":
            for bound in sorted(_KEYWORDS["integer"]):
                value = node.get(bound)
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError(
                        f"fields schema {path}: fractional integer bound {bound!r} is unsupported"
                    )
        if kind == "object":
            if node.get("additionalProperties", True) not in (True, False):
                raise ValueError(
                    f"fields schema {path}: schema-valued additionalProperties is unsupported"
                )
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError(f"fields schema {path}: properties must be an object")
            missing = set(node.get("required", [])) - set(properties)
            if missing:
                raise ValueError(f"fields schema {path}: required properties must be defined")
            for key, child in properties.items():
                visit(child, f"{path}.properties[{key!r}]")
        elif kind == "array" and "items" in node:
            visit(node["items"], f"{path}.items")

    def validate_default_types(node: dict[str, Any], value: object, path: str) -> None:
        # JSON Schema accepts 1.0 as an integer, while a strict Pydantic int does not.
        if node.get("type") == "integer" and isinstance(value, float):
            raise ValueError(f"fields schema {path}: integer default must use an integer value")
        if node.get("type") == "object" and isinstance(value, dict):
            properties = node.get("properties", {})
            for key, child in value.items():
                if isinstance(properties.get(key), dict):
                    validate_default_types(properties[key], child, f"{path}.{key}")
        if node.get("type") == "array" and isinstance(value, list):
            items = node.get("items")
            if isinstance(items, dict):
                for index, child in enumerate(value):
                    validate_default_types(items, child, f"{path}[{index}]")

    finite(schema, "$")
    visit(schema, "$")


def _display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))


def _step(schema: dict[str, Any]) -> str:
    classes: list[str] = []
    _model(schema, "ActionInput", classes)
    return (
        '"""Example Action; replace its implementation with your own."""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Annotated, Any, Literal\n\n"
        "from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema\n"
        "from syntara_plugin.runtime import ActionStep, ExecutionContext\n\n\n"
        + "\n\n\n".join(classes)
        + "\n\n\nclass ActionOutput(BaseModel):\n"
        '    model_config = ConfigDict(extra="forbid")\n\n    resource: str\n\n\n'
        "class ActionImplementation(ActionStep[ActionInput, ActionOutput]):\n"
        "    def run(self, inputs: ActionInput, context: ExecutionContext) -> ActionOutput:\n"
        "        return ActionOutput(resource=inputs.resource)\n"
    )


def _model(schema: dict[str, Any], name: str, classes: list[str]) -> None:
    properties = schema.get("properties", {})
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required", [])
    lines = [
        f"class {name}(BaseModel):",
        '    model_config = ConfigDict(strict=True, allow_inf_nan=False, regex_engine="python-re", extra='
        + ('"forbid"' if schema.get("additionalProperties") is False else '"allow"')
        + ")",
    ]
    used: set[str] = set()
    for index, (key, raw) in enumerate(properties.items()):
        prop = raw if isinstance(raw, dict) else {}
        field = key if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", key) else f"field_{index}"
        if (
            keyword.iskeyword(field)
            or field.startswith("model_")
            or field.startswith("ActionInput")
            or field
            in {"Annotated", "Any", "Literal", "BaseModel", "ConfigDict", "Field", "WithJsonSchema"}
            or hasattr(builtins, field)
            or hasattr(BaseModel, field)
            or field in used
        ):
            field = f"field_{index}"
        while field in used:
            field += "_"
        used.add(field)
        annotation = _annotation(prop, f"{name}Field{index}", classes)
        optional = key not in required
        default = (
            repr(prop["default"])
            if optional and "default" in prop
            else ("None" if optional else "...")
        )
        constraints = _constraints(prop)
        validate_default = ", validate_default=True" if optional and "default" in prop else ""
        lines.append(
            f"    {field}: {annotation} = Field({default}, alias={key!r}"
            f"{constraints}{validate_default})"
        )
    classes.append("\n".join(lines))


def _annotation(schema: dict[str, Any], name: str, classes: list[str]) -> str:
    kind = schema.get("type")
    if "enum" in schema:
        return f"Literal[{', '.join(repr(value) for value in schema['enum'])}]"
    if kind == "number":
        # Preserve large integers at runtime without exposing a union to drift checks.
        number_schema = {key: value for key, value in schema.items() if key in _KEYWORDS["number"]}
        number_schema["type"] = "number"
        return f"Annotated[int | float, WithJsonSchema({number_schema!r})]"
    if kind == "object":
        _model(schema, name, classes)
        return name
    if kind == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            item = _annotation(items, name + "Item", classes)
            constraints = _constraints(items)
            if constraints:
                item = f"Annotated[{item}, Field({constraints[2:]})]"
        else:
            item = "Any"
        return f"list[{item}]"
    return {
        "string": "str",
        "integer": "int",
        "number": "int | float",
        "boolean": "bool",
        "object": "dict[str, Any]",
        "null": "None",
    }.get(kind, "Any")


def _constraints(schema: dict[str, Any]) -> str:
    return "".join(
        f", {argument}={_constraint_value(schema, keyword)!r}"
        for keyword, argument in _FIELD_CONSTRAINTS.items()
        if keyword in schema
    )


def _constraint_value(schema: dict[str, Any], keyword: str) -> object:
    value = schema[keyword]
    if schema.get("type") == "integer" and isinstance(value, float):
        return int(value)
    return value


def _runtime(package: str, target: str, input_path: str) -> str:
    return f'''"""Temporary single-Action adapter pending the binding helper."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from syntara_plugin.runtime import InstalledStepManifest, StepBindings, StepDescriptor

from .step import ActionImplementation


def _read(path: str) -> dict[str, Any]:
    packaged = files({package!r}).joinpath("manifests", *path.split("/"))
    if packaged.is_file():
        return json.loads(packaged.read_text(encoding="utf-8"))
    # Source checkout: the wheel includes these same files as package resources.
    return json.loads((Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8"))


def create_bindings() -> StepBindings:
    """Bind this scaffold's one Action using its packaged manifest and input asset."""
    plugin = _read("plugin.yaml")
    target = _read({target!r})
    identity = ".".join((plugin["metadata"]["namespace"], plugin["metadata"]["name"],
                         target["metadata"]["name"]))
    installed = InstalledStepManifest.from_manifest(identity, target, {{{input_path!r}: _read({input_path!r})}})
    step = ActionImplementation()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_installed_step(step, installed), step)
    return bindings
'''


def _pyproject(name: str, version: str, package: str) -> str:
    release, separator, prerelease = version.partition("-")
    local = re.sub(r"[^a-zA-Z0-9]+", ".", prerelease).strip(".")
    python_version = f"{release}.dev0+{local or 'prerelease'}" if separator else release
    return f'''[build-system]
requires = ["hatchling==1.32.4"]
build-backend = "hatchling.build"

[project]
name = "syntara-{name}"
version = "{python_version}"
requires-python = ">=3.12"
dependencies = ["syntara-plugin-contracts>=0.1.0a1"]

[tool.hatch.build.targets.wheel]
packages = ["{package}"]

[tool.hatch.build.targets.wheel.force-include]
"plugin.yaml" = "{package}/manifests/plugin.yaml"
"steps" = "{package}/manifests/steps"
"schemas" = "{package}/manifests/schemas"
'''


def _containerfile() -> str:
    return f"""ARG SYNTARA_RUNTIME_BASE_IMAGE={_BASE_IMAGE}
FROM ${{SYNTARA_RUNTIME_BASE_IMAGE}}
WORKDIR /app
COPY wheels /wheels
RUN python3.12 -m pip install --no-cache-dir --only-binary :all: /wheels/*.whl
USER 0
RUN mkdir -p /etc/syntara
COPY runtime.json /etc/syntara/runtime.json
RUN chmod 0644 /etc/syntara/runtime.json
ENV SYNTARA_RUNTIME_CONFIG=/etc/syntara/runtime.json \\
    SYNTARA_RUNTIME_GRPC_ADDRESS=0.0.0.0:50051 \\
    SYNTARA_RUNTIME_GRPC_TLS_CERT=/run/secrets/server.crt \\
    SYNTARA_RUNTIME_GRPC_TLS_KEY=/run/secrets/server.key \\
    SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/run/secrets/client-ca.crt \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1
USER 1001
EXPOSE 50051
ENTRYPOINT ["python3.12", "-m", "syntara_plugin.runtime.server"]
"""


def _readme(name: str, action: str) -> str:
    return f"""# {_display_name(name)}

This workspace contains one custom-runner Action, `{action}`. Replace the example
implementation and review its schemas and generated tests before publishing.

The generated input model supports string enums and bounds, Python-compatible
patterns, numeric bounds, and array length bounds. Unsupported schema keywords,
including formats, and numbers requiring scientific notation are rejected during
scaffolding.

```sh
syntara-plugin validate
syntara-plugin run {action} --input '{{"resource":"example"}}'
pytest tests -q
```

Before building the image, prepare `wheels/` with this package and its runtime
dependencies, for example `python -m pip wheel --wheel-dir wheels .`. Configure a
workload registry in `syntara-plugin.yaml` before using `build --with-workload`.

The single-Action `runtime.py` binding adapter is temporary pending the public
packaged-manifest binding helper. It reads the scaffold's JSON-formatted YAML
manifests and input asset; update it when adding targets or changing their paths.
"""
