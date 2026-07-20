"""Pure role-mapping for LTI 1.3 ``roles`` claim URIs.

The LTI 1.3 spec (IMS Global, *Learning Tools Interoperability
Advantage*, §"Role vocabularies") defines a fixed set of role URIs
of the form ``http://purl.imsglobal.org/vocab/lis/v2/...`` and
``https://purl.imsglobal.org/vocab/lti/...``. A launch may carry
several roles — a student who is also a teaching assistant, for
example — and the *highest* applicable role wins for routing.

This module is the single place that knows the LTI 1.3 role URI
list. The rest of the system speaks the application-level role
enum :class:`AppRole` and never sees the raw URI.
"""

from __future__ import annotations

import enum
from typing import Iterable


# Canonical LTI 1.3 role URIs we recognize. Anything outside this
# table is an error — silently treating an unknown role as
# "learner" would be a privilege-escalation risk.
_LTI_SYSTEM_ROLES = frozenset(
    {
        # Core roles.
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator",
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysAdmin",
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysSupport",
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#None",
    }
)

_LTI_INSTITUTION_ROLES = frozenset(
    {
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Faculty",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Instructor",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#TeachingAssistant",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#ContentDeveloper",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Member",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Learner",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Student",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Staff",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Alumni",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#ProspectiveStudent",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Guest",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Other",
    }
)

_LTI_CONTEXT_ROLES = frozenset(
    {
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#TeachingAssistant",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#ContentDeveloper",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Member",
    }
)

# The LTI Proctor role is an extension URI defined by the 1EdTech
# proctoring profile draft. It is the only non-core URI we accept.
_LTI_PROCTOR_ROLES = frozenset(
    {
        "http://purl.imsglobal.org/vocab/lti/role/proctor#Proctor",
    }
)

_KNOWN_ROLES = _LTI_SYSTEM_ROLES | _LTI_INSTITUTION_ROLES | _LTI_CONTEXT_ROLES | _LTI_PROCTOR_ROLES


class AppRole(str, enum.Enum):
    """The application-level role a launch maps to.

    The enum value is the string stored in the session token
    (``role`` claim) and in :attr:`proctoring_engine.models.AdminUser.role`.
    """

    LEARNER = "learner"
    INSTRUCTOR = "instructor"
    ADMIN = "admin"
    PROCTOR = "proctor"


_ROLE_TO_APP = {
    # Admin (highest privilege).
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator": AppRole.ADMIN,
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysAdmin": AppRole.ADMIN,
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#SysSupport": AppRole.ADMIN,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator": AppRole.ADMIN,
    # Proctor (extension URI, above instructor for routing purposes).
    "http://purl.imsglobal.org/vocab/lti/role/proctor#Proctor": AppRole.PROCTOR,
    # Instructor / faculty / TA / content developer.
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Faculty": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Instructor": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#TeachingAssistant": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#ContentDeveloper": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#TeachingAssistant": AppRole.INSTRUCTOR,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#ContentDeveloper": AppRole.INSTRUCTOR,
    # Learner / student (lowest privilege).
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Learner": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Student": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/membership#Member": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Member": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Staff": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Alumni": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#ProspectiveStudent": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Guest": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Other": AppRole.LEARNER,
    "http://purl.imsglobal.org/vocab/lis/v2/system/person#None": AppRole.LEARNER,
}


# Privilege ordering — higher means more privileged. Used to break
# ties when a launch carries multiple roles.
_PRIVILEGE = {
    AppRole.LEARNER: 0,
    AppRole.INSTRUCTOR: 1,
    AppRole.PROCTOR: 2,
    AppRole.ADMIN: 3,
}


def map_roles(role_uris: Iterable[str]) -> AppRole:
    """Reduce a list of LTI role URIs to a single :class:`AppRole`.

    The highest-privilege role wins. A launch carrying both an
    ``Instructor`` and a ``SysAdmin`` role resolves to ``ADMIN``; a
    launch carrying both a ``Learner`` and a ``TeachingAssistant``
    role resolves to ``INSTRUCTOR``.

    Raises:
        ValueError: ``role_uris`` is empty, or contains a URI not in
            the recognized LTI 1.3 role vocabulary. Treating an
            unknown role as a learner would be a silent privilege
            *escalation* (or, more often, a silent failure that
            routes a student into the admin surface); the safe move
            is to fail loud.
    """

    uris = list(role_uris)
    if not uris:
        raise ValueError("LTI roles claim must contain at least one URI")
    unknown = [uri for uri in uris if uri not in _KNOWN_ROLES]
    if unknown:
        raise ValueError(
            f"unrecognized LTI role URI(s): {sorted(unknown)}"
        )
    mapped = [_ROLE_TO_APP[uri] for uri in uris]
    return max(mapped, key=lambda role: _PRIVILEGE[role])


def is_admin_route(role: AppRole) -> bool:
    """Return whether a launch with this role should route to the
    admin surface rather than the exam client.
    """

    return role in (AppRole.ADMIN, AppRole.INSTRUCTOR, AppRole.PROCTOR)
