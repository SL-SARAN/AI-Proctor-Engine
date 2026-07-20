"""Unit tests for the LTI 1.3 ``id_token`` Pydantic models.

Boundary cases (per ``docs/08-test-strategy-design.md`` §"Ingestion
layer"): missing required claim, wrong ``message_type``, wrong
``version``, empty ``roles`` list, malformed ``custom`` dict,
non-object payload.
"""

from __future__ import annotations

import pytest

from proctoring_engine.lti.claims import (
    CLAIM_CUSTOM,
    CLAIM_DEPLOYMENT_ID,
    CLAIM_VERSION,
    LTI_MESSAGE_TYPE_DEEP_LINKING,
    LTI_MESSAGE_TYPE_RESOURCE_LINK,
    LTI_VERSION_1_3,
    LtiClaimsError,
    LtiContext,
    LtiCustomClaims,
    LtiIdToken,
    LtiResourceLink,
    LtiToolPlatform,
    combined_context_id,
    require_policy_config_name,
)


def _valid_payload() -> dict:
    """Return a payload that should pass LTI claim validation."""

    return {
        "iss": "https://lms.example.edu",
        "sub": "user-12345",
        "aud": "proctoring-engine",
        "exp": 9999999999,
        "iat": 1700000000,
        "nonce": "nonce-value",
        "https://purl.imsglobal.org/spec/lti/claim/message_type": LTI_MESSAGE_TYPE_RESOURCE_LINK,
        "https://purl.imsglobal.org/spec/lti/claim/version": LTI_VERSION_1_3,
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id": "deployment-1",
        "https://purl.imsglobal.org/spec/lti/claim/roles": [
            "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
        ],
        "https://purl.imsglobal.org/spec/lti/claim/context": {
            "id": "course-101",
            "label": "CS101",
            "title": "Intro to CS",
        },
        "https://purl.imsglobal.org/spec/lti/claim/resource_link": {
            "id": "exam-midterm",
            "title": "Midterm",
        },
        "https://purl.imsglobal.org/spec/lti/claim/tool_platform": {
            "guid": "lms-guid",
            "name": "Example LMS",
            "product_family_code": "example",
        },
        "https://purl.imsglobal.org/spec/lti/claim/target_link_uri": (
            "http://localhost:8000/lti/launch"
        ),
        "https://purl.imsglobal.org/spec/lti/claim/custom": {
            "policy_config_name": "cs101-default"
        },
    }


def test_valid_payload_parses() -> None:
    """A spec-compliant payload produces a fully-typed model."""

    claims = LtiIdToken.from_jwt_payload(_valid_payload())
    assert claims.issuer == "https://lms.example.edu"
    assert claims.subject == "user-12345"
    assert claims.audience == "proctoring-engine"
    assert claims.message_type == LTI_MESSAGE_TYPE_RESOURCE_LINK
    assert claims.version == LTI_VERSION_1_3
    assert claims.deployment_id == "deployment-1"
    assert claims.roles == [
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
    ]
    assert claims.context.id == "course-101"
    assert claims.resource_link.id == "exam-midterm"
    assert claims.tool_platform.guid == "lms-guid"
    assert claims.custom.policy_config_name == "cs101-default"


def test_missing_required_claim_raises() -> None:
    """Removing any required claim produces a typed validation error."""

    payload = _valid_payload()
    payload.pop(CLAIM_DEPLOYMENT_ID)
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_wrong_version_raises() -> None:
    """A non-1.3.0 version is rejected at the parse boundary."""

    payload = _valid_payload()
    payload[CLAIM_VERSION] = "9.9.9"
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_wrong_message_type_raises() -> None:
    """An unknown LTI 1.3 message type is rejected at the parse boundary."""

    payload = _valid_payload()
    payload["https://purl.imsglobal.org/spec/lti/claim/message_type"] = (
        "LtiBogusRequest"
    )
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_require_lti_version_1_3_rejects_other_versions() -> None:
    """``require_lti_version_1_3`` is a hard check, not a coercion.

    The parse boundary also rejects non-1.3.0 versions — see
    :func:`test_wrong_version_raises` — so this test exercises the
    explicit method on a directly-constructed model (bypassing the
    parse path) to lock the method's contract in isolation.

    ``model_construct`` skips validation, which is the point here:
    we want to *bypass* the parse path's check so we can prove the
    explicit method still rejects a bad version on its own.
    """

    claims = LtiIdToken.model_construct(
        issuer="https://lms.example.edu",
        subject="user-1",
        audience="proctoring-engine",
        expires_at=9999999999,
        issued_at=1700000000,
        nonce="nonce-1",
        message_type=LTI_MESSAGE_TYPE_RESOURCE_LINK,
        version=LTI_VERSION_1_3,
        deployment_id="dep-1",
        roles=["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"],
        context=LtiContext(id="c1"),
        resource_link=LtiResourceLink(id="r1"),
        tool_platform=LtiToolPlatform(guid="g", name="n", product_family_code="p"),
        target_link_uri="http://localhost:8000/lti/launch",
        custom=LtiCustomClaims(),
        name=None,
        email=None,
        preferred_username=None,
    )
    claims.require_lti_version_1_3()

    # Mutate the version (the typed model is the contract; the
    # input was already validated at the parse boundary).
    object.__setattr__(claims, "version", "1.2.0")
    with pytest.raises(LtiClaimsError):
        claims.require_lti_version_1_3()


def test_require_resource_link_request_accepts_resource_link() -> None:
    """A resource-link launch passes ``require_resource_link_request``."""

    claims = LtiIdToken.from_jwt_payload(_valid_payload())
    claims.require_resource_link_request()


def test_require_resource_link_request_rejects_deep_linking() -> None:
    """Deep-linking launches are rejected at the parse boundary.

    The deep-linking message type is recognized by the validator as
    a *known* LTI 1.3 message type, but v1 only accepts
    resource-link launches, so the parse path surfaces it as an
    ``LtiClaimsError`` before the typed model is built.
    """

    payload = _valid_payload()
    payload["https://purl.imsglobal.org/spec/lti/claim/message_type"] = (
        LTI_MESSAGE_TYPE_DEEP_LINKING
    )
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_empty_roles_list_raises() -> None:
    """The roles claim must contain at least one URI."""

    payload = _valid_payload()
    payload["https://purl.imsglobal.org/spec/lti/claim/roles"] = []
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_custom_policy_config_name_must_be_non_empty() -> None:
    """The policy-lookup custom claim must be a non-empty string."""

    payload = _valid_payload()
    payload[CLAIM_CUSTOM] = {"policy_config_name": ""}
    claims = LtiIdToken.from_jwt_payload(payload)
    with pytest.raises(LtiClaimsError):
        require_policy_config_name(claims)


def test_custom_claim_can_be_omitted_for_non_policy_launches() -> None:
    """A launch without a custom claim parses; only ``require_policy_config_name`` raises."""

    payload = _valid_payload()
    payload[CLAIM_CUSTOM] = {}
    claims = LtiIdToken.from_jwt_payload(payload)
    assert claims.custom.policy_config_name is None
    with pytest.raises(LtiClaimsError):
        require_policy_config_name(claims)


def test_non_object_payload_raises() -> None:
    """A non-object JWT payload is rejected."""

    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload("not-a-dict")  # type: ignore[arg-type]


def test_extra_lti_claim_raises() -> None:
    """An unknown LTI-namespaced claim is rejected (strict validation)."""

    payload = _valid_payload()
    payload["https://example.com/an-extra-claim"] = "ignored"
    with pytest.raises(LtiClaimsError):
        LtiIdToken.from_jwt_payload(payload)


def test_combined_context_id_format() -> None:
    """The combined context id is ``<context.id>:<resource_link.id>``."""

    claims = LtiIdToken.from_jwt_payload(_valid_payload())
    assert combined_context_id(claims) == "course-101:exam-midterm"


def test_sub_models_reject_unknown_fields() -> None:
    """Sub-models use ``extra=forbid`` so a stray field is rejected.

    ``LtiCustomClaims`` is excluded because it uses ``extra=allow``
    by design — the platform may pass through arbitrary custom
    claims that v1 simply ignores.
    """

    with pytest.raises(Exception):  # pydantic.ValidationError
        LtiContext(id="c1", not_a_real_field="oops")
    with pytest.raises(Exception):
        LtiResourceLink(id="r1", not_a_real_field="oops")
    with pytest.raises(Exception):
        LtiToolPlatform(
            guid="g",
            name="n",
            product_family_code="p",
            not_a_real_field="oops",
        )
