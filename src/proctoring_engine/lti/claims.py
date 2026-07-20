"""Pydantic v2 models that describe the shape of an LTI 1.3 ``id_token``.

The LTI 1.3 spec uses a set of namespaced claim URIs as keys
(``https://purl.imsglobal.org/spec/lti/claim/...``). Python field
names cannot carry those URIs directly, so the models use shortened
identifiers and the namespacing is applied in the :meth:`LtiIdToken.
from_jwt_payload` class method.

Validation is strict: any unknown LTI claim raises
:class:`ValueError`. The spec's contract is explicit about which
claims are required for a resource-link launch, and silently
ignoring an unexpected claim (e.g. a future IMS profile) would
make forward-compatibility harder to reason about.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# --- Claim URI constants ----------------------------------------------

#: Standard OIDC claims (RFC 7519 §4).
CLAIM_ISSUER = "iss"
CLAIM_SUBJECT = "sub"
CLAIM_AUDIENCE = "aud"
CLAIM_EXPIRES_AT = "exp"
CLAIM_ISSUED_AT = "iat"
CLAIM_NONCE = "nonce"

#: LTI 1.3 namespaced claims.
CLAIM_MESSAGE_TYPE = "https://purl.imsglobal.org/spec/lti/claim/message_type"
CLAIM_VERSION = "https://purl.imsglobal.org/spec/lti/claim/version"
CLAIM_DEPLOYMENT_ID = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
CLAIM_ROLES = "https://purl.imsglobal.org/spec/lti/claim/roles"
CLAIM_CONTEXT = "https://purl.imsglobal.org/spec/lti/claim/context"
CLAIM_RESOURCE_LINK = "https://purl.imsglobal.org/spec/lti/claim/resource_link"
CLAIM_TOOL_PLATFORM = "https://purl.imsglobal.org/spec/lti/claim/tool_platform"
CLAIM_CUSTOM = "https://purl.imsglobal.org/spec/lti/claim/custom"
CLAIM_TARGET_LINK_URI = "https://purl.imsglobal.org/spec/lti/claim/target_link_uri"
CLAIM_LAUNCH_PRESENTATION = "https://purl.imsglobal.org/spec/lti/claim/launch_presentation"

#: Standard OIDC profile claims.
CLAIM_NAME = "name"
CLAIM_EMAIL = "email"
CLAIM_PREFERRED_USERNAME = "preferred_username"

#: The one v1 message type we accept. ``LtiDeepLinkingRequest`` is
#: recognized by the validator but is reserved for a future layer.
LTI_MESSAGE_TYPE_RESOURCE_LINK = "LtiResourceLinkRequest"
LTI_MESSAGE_TYPE_DEEP_LINKING = "LtiDeepLinkingRequest"

#: The single LTI version string v1 accepts.
LTI_VERSION_1_3 = "1.3.0"

#: The custom-claim key that names the ``PolicyConfig`` snapshot
#: the launch should bind. The platform sets this in the LTI
#: custom-claims configuration; the launch handler reads it.
CUSTOM_POLICY_CONFIG_NAME = "policy_config_name"


# --- Sub-models -------------------------------------------------------


class LtiContext(BaseModel):
    """The LTI 1.3 ``context`` claim — the course or other
    organizational unit the launch is happening in.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    label: Optional[str] = Field(default=None, max_length=512)
    title: Optional[str] = Field(default=None, max_length=512)
    type: Optional[list[str]] = None


class LtiResourceLink(BaseModel):
    """The LTI 1.3 ``resource_link`` claim — the specific exam
    resource the launch is targeting.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    title: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = None
    url: Optional[str] = None


class LtiToolPlatform(BaseModel):
    """The LTI 1.3 ``tool_platform`` claim — the LMS / platform
    that issued the launch.
    """

    model_config = ConfigDict(extra="forbid")

    guid: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=512)
    product_family_code: str = Field(min_length=1, max_length=128)
    version: Optional[str] = None
    url: Optional[str] = None


class LtiCustomClaims(BaseModel):
    """The LTI 1.3 ``custom`` claim.

    v1 reads exactly one field — :attr:`policy_config_name` — from
    this object. The model is ``extra=allow`` so the platform can
    pass through other custom values without us rejecting the
    launch; the launch service ignores them. The
    ``policy_config_name`` field is ``Optional`` with no length
    constraint at the model level: the empty-string check is the
    job of :func:`require_policy_config_name`, not the schema, so
    a launch that omits the claim still parses.
    """

    model_config = ConfigDict(extra="allow")

    policy_config_name: Optional[str] = Field(default=None, max_length=128)


# --- The launch claim envelope ---------------------------------------


class LtiIdToken(BaseModel):
    """The validated claim set of an LTI 1.3 resource-link launch.

    The model is built from a JWT payload by
    :meth:`from_jwt_payload`, which does the namespacing. Field
    names are the short identifiers; the on-the-wire URIs are not
    present in the model.
    """

    model_config = ConfigDict(extra="forbid")

    # Standard OIDC claims.
    issuer: str = Field(alias=CLAIM_ISSUER, min_length=1, max_length=2048)
    subject: str = Field(alias=CLAIM_SUBJECT, min_length=1, max_length=2048)
    audience: str = Field(alias=CLAIM_AUDIENCE, min_length=1, max_length=2048)
    expires_at: int = Field(alias=CLAIM_EXPIRES_AT)
    issued_at: int = Field(alias=CLAIM_ISSUED_AT)
    nonce: str = Field(alias=CLAIM_NONCE, min_length=1, max_length=2048)

    # LTI namespaced claims.
    message_type: str = Field(alias=CLAIM_MESSAGE_TYPE)
    version: str = Field(alias=CLAIM_VERSION)
    deployment_id: str = Field(alias=CLAIM_DEPLOYMENT_ID, min_length=1, max_length=512)
    roles: list[str] = Field(alias=CLAIM_ROLES, min_length=1)
    context: LtiContext = Field(alias=CLAIM_CONTEXT)
    resource_link: LtiResourceLink = Field(alias=CLAIM_RESOURCE_LINK)
    tool_platform: LtiToolPlatform = Field(alias=CLAIM_TOOL_PLATFORM)
    target_link_uri: str = Field(alias=CLAIM_TARGET_LINK_URI, min_length=1, max_length=2048)
    custom: LtiCustomClaims = Field(alias=CLAIM_CUSTOM, default_factory=LtiCustomClaims)

    # Standard OIDC profile claims (optional).
    name: Optional[str] = Field(alias=CLAIM_NAME, default=None, max_length=512)
    email: Optional[str] = Field(alias=CLAIM_EMAIL, default=None, max_length=512)
    preferred_username: Optional[str] = Field(
        alias=CLAIM_PREFERRED_USERNAME, default=None, max_length=512
    )

    @classmethod
    def from_jwt_payload(cls, payload: dict[str, Any]) -> "LtiIdToken":
        """Build an :class:`LtiIdToken` from a verified JWT payload.

        The payload is the decoded body of the ``id_token`` after
        signature, ``exp``, ``iss``, and ``aud`` have been
        validated. This method does not re-validate those — it
        constructs the typed view, runs Pydantic validation, and
        then applies the LTI-version and message-type checks so a
        wrong version or message type is caught at the parse
        boundary rather than left to the route handler to remember.

        Raises:
            LtiClaimsError: A required claim is missing, has an
                invalid value, the LTI version is not ``1.3.0``,
                or the message type is not a recognized LTI 1.3
                message type.
        """

        if not isinstance(payload, dict):
            raise LtiClaimsError("id_token payload is not a JSON object")

        try:
            instance = cls.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 — surface a typed error
            raise LtiClaimsError(f"LTI id_token claims are invalid: {exc}") from exc

        # Apply the LTI-version and message-type checks at the
        # boundary so the typed model is *never* built with an
        # invalid version or unsupported message type.
        instance.require_lti_version_1_3()
        instance.require_resource_link_request()
        return instance

    def require_resource_link_request(self) -> None:
        """Reject non-resource-link message types.

        The v1 launch handler accepts only ``LtiResourceLinkRequest``.
        ``LtiDeepLinkingRequest`` is a v2 concern; the validator
        surfaces it as a distinct error so a future layer can plug
        in support without changing this method.
        """

        if self.message_type == LTI_MESSAGE_TYPE_RESOURCE_LINK:
            return
        if self.message_type == LTI_MESSAGE_TYPE_DEEP_LINKING:
            raise LtiClaimsError(
                "LtiDeepLinkingRequest is not supported in v1; "
                "the next layer will reject this in the route handler"
            )
        raise LtiClaimsError(
            f"unsupported LTI message_type {self.message_type!r}; "
            f"v1 only accepts {LTI_MESSAGE_TYPE_RESOURCE_LINK!r}"
        )

    def require_lti_version_1_3(self) -> None:
        """Reject any LTI version other than ``1.3.0``."""

        if self.version != LTI_VERSION_1_3:
            raise LtiClaimsError(
                f"LTI version must be {LTI_VERSION_1_3!r}, got {self.version!r}"
            )


class LtiClaimsError(Exception):
    """Raised when an ``id_token`` payload fails LTI claim validation."""


# --- Helpers ----------------------------------------------------------


def require_policy_config_name(claims: LtiIdToken) -> str:
    """Return the policy name from a validated launch.

    Raises :class:`LtiClaimsError` when the platform did not set the
    custom-claim value, or when the value is the empty string.
    """

    name = claims.custom.policy_config_name
    if not name:
        raise LtiClaimsError(
            f"custom claim '{CUSTOM_POLICY_CONFIG_NAME}' is required and must be non-empty"
        )
    return name


def combined_context_id(claims: LtiIdToken) -> str:
    """Return the combined ``<context.id>:<resource_link.id>`` value.

    The launch handler uses this as
    :attr:`proctoring_engine.models.ExamSession.lti_context_id` so
    one ``ExamSession`` is created per (course, exam) pair per
    platform, matching the v1 spec's "one exam per session" model.
    """

    return f"{claims.context.id}:{claims.resource_link.id}"
