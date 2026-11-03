"""Typed views of the canonical v1alpha1 credential authoring documents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from syntara_plugin.contracts import canonical_json_bytes, load_bundle, validate_document


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, serialize_by_alias=True)


class TargetMetadata(_Model):
    name: str
    display_name: str = Field(alias="displayName")
    description: str


class InlineDocument(_Model):
    kind: Literal["inline"]
    value: dict[str, Any]


class AssetDocument(_Model):
    kind: Literal["asset"]
    path: str


DocumentSource = InlineDocument | AssetDocument


class StaticBearerIssuance(_Model):
    kind: Literal["static-bearer.v1"]


class OAuthAuthorizationCodeIssuance(_Model):
    kind: Literal["oauth2-authorization-code.v1"]
    scopes: list[str]
    pkce: Literal[True]


class JwtTokenExchangeIssuance(_Model):
    kind: Literal["jwt-token-exchange.v1"]
    audience: str


Issuance = StaticBearerIssuance | OAuthAuthorizationCodeIssuance | JwtTokenExchangeIssuance


class CredentialRecipeSpec(_Model):
    id: str
    integration_type: str = Field(alias="integrationType")
    credential_schema: DocumentSource = Field(alias="credentialSchema", discriminator="kind")
    secret_fields: list[str] = Field(alias="secretFields")
    issuance: Issuance = Field(discriminator="kind")


class IntegrationTypeSpec(_Model):
    id: str
    endpoint_kinds: list[str] = Field(alias="endpointKinds")
    credential_types: list[str] = Field(alias="credentialTypes")
    configuration: DocumentSource = Field(discriminator="kind")


def _check_contract(value: Any, kind: Literal["credential_recipe", "integration"]) -> Any:
    try:
        canonical_json_bytes(value)
        errors = validate_document(load_bundle(), kind, value)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("document must satisfy the canonical contract") from None
    if errors:
        raise ValueError("document must satisfy the canonical contract")
    return value


class CredentialRecipe(_Model):
    """The canonical CredentialRecipe document, not a separate manifest."""

    api_version: Literal["syntara.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["CredentialRecipe"]
    metadata: TargetMetadata
    spec: CredentialRecipeSpec

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        return _check_contract(value, "credential_recipe")


class IntegrationType(_Model):
    """The canonical IntegrationType document."""

    api_version: Literal["syntara.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["IntegrationType"]
    metadata: TargetMetadata
    spec: IntegrationTypeSpec

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, value: Any) -> Any:
        return _check_contract(value, "integration")
