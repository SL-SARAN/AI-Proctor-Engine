"""Unit tests for the LTI 1.3 role mapper.

Boundary cases: each recognized role URI maps to the expected
:class:`AppRole`; an unrecognized URI is an error; a multi-role
launch reduces to the highest-privilege role; an empty list is an
error.
"""

from __future__ import annotations

import pytest

from proctoring_engine.lti.roles import (
    AppRole,
    is_admin_route,
    map_roles,
)


def test_learner_uri_maps_to_learner() -> None:
    """The core ``Learner`` role resolves to the learner surface."""

    assert (
        map_roles(["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"])
        == AppRole.LEARNER
    )
    assert not is_admin_route(AppRole.LEARNER)


def test_student_uri_maps_to_learner() -> None:
    """``Student`` (institution role) also resolves to the learner surface."""

    assert (
        map_roles(["http://purl.imsglobal.org/vocab/lis/v2/institution/person#Student"])
        == AppRole.LEARNER
    )


def test_instructor_uri_maps_to_instructor() -> None:
    """``Instructor`` resolves to the instructor surface (admin route)."""

    assert (
        map_roles(["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"])
        == AppRole.INSTRUCTOR
    )
    assert is_admin_route(AppRole.INSTRUCTOR)


def test_faculty_uri_maps_to_instructor() -> None:
    """``Faculty`` (institution role) resolves to instructor."""

    assert (
        map_roles(
            ["http://purl.imsglobal.org/vocab/lis/v2/institution/person#Faculty"]
        )
        == AppRole.INSTRUCTOR
    )


def test_administrator_uri_maps_to_admin() -> None:
    """``Administrator`` (system role) resolves to admin (highest privilege)."""

    assert (
        map_roles(
            [
                "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator"
            ]
        )
        == AppRole.ADMIN
    )
    assert is_admin_route(AppRole.ADMIN)


def test_sysadmin_uri_maps_to_admin() -> None:
    """``SysAdmin`` resolves to admin."""

    assert (
        map_roles(["http://purl.imsglobal.org/vocab/lis/v2/system/person#SysAdmin"])
        == AppRole.ADMIN
    )


def test_proctor_uri_maps_to_proctor() -> None:
    """The LTI proctoring profile URI resolves to proctor."""

    assert (
        map_roles(["http://purl.imsglobal.org/vocab/lti/role/proctor#Proctor"])
        == AppRole.PROCTOR
    )
    assert is_admin_route(AppRole.PROCTOR)


def test_multi_role_takes_highest_privilege() -> None:
    """When a launch carries multiple roles, the highest-privilege
    role wins.
    """

    uris = [
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysAdmin",
    ]
    assert map_roles(uris) == AppRole.ADMIN


def test_multi_role_learner_plus_ta_resolves_to_instructor() -> None:
    """A student-TA combination resolves to instructor (the higher role)."""

    uris = [
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#TeachingAssistant",
    ]
    assert map_roles(uris) == AppRole.INSTRUCTOR


def test_empty_role_list_raises() -> None:
    """An empty ``roles`` claim is rejected."""

    with pytest.raises(ValueError):
        map_roles([])


def test_unknown_role_uri_raises() -> None:
    """An unrecognized role URI is rejected (silent fallback would
    be a privilege-escalation risk).
    """

    with pytest.raises(ValueError):
        map_roles(
            ["http://example.com/not-a-lti-role"]
        )


def test_mixed_known_and_unknown_uri_raises() -> None:
    """A single unknown URI in a list poisons the whole launch."""

    uris = [
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
        "http://example.com/not-a-lti-role",
    ]
    with pytest.raises(ValueError):
        map_roles(uris)
