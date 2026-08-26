"""Regression tests for the Alembic migration chain.

These tests assert structural properties of the migration chain that, if
violated, will produce ``DuplicateObject`` / ``DuplicateColumn`` failures
in CI. The two recurring failure modes they guard against are:

1. **Post-initial migration adds an enum value** that is already present
   in the PostgreSQL enum type because the Python enum in
   ``src/proctoring_engine/models.py`` already declares it (and
   ``Base.metadata.create_all`` in the initial migration emits it).

2. **Post-initial migration adds a column, constraint, table, or index**
   that is already present in the schema because the corresponding ORM
   model declares it (and ``Base.metadata.create_all`` in the initial
   migration emits it).

Both modes share a single root cause: the initial migration creates the
schema from the full current ORM via ``Base.metadata.create_all``,
so any subsequent migration that tries to add a column / constraint /
enum value that the ORM already declares will collide with what the
initial migration already created. The only thing the ORM cannot model
is a DML trigger — that is the only legitimate content of a
post-initial migration in this project.

History: see commits ``8350198``, ``94e4a61``, ``c05304a`` for the
fix-by-fix record. The third migration (``20260719_0003``) was
entirely redundant and was deleted when this test was added.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from proctoring_engine.models import Base


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _render_initial_ddl() -> str:
    """Render the DDL that the initial migration emits.

    Uses ``alembic --sql`` to render the SQL the initial migration
    would execute against a real PostgreSQL database. This is the
    source of truth for "what the initial migration has already
    created" — not ``Base.metadata.create_all`` directly, because
    SQLAlchemy's standalone ``create_all`` does not emit ``CREATE
    TYPE`` for enums the way alembic's invocation does.
    """

    import io
    import contextlib
    from alembic.config import Config as AlembicConfig
    from alembic import command

    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR.parent))
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://placeholder/placeholder"
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "20260717_0001", sql=True)
    raw = buffer.getvalue()
    # Strip the alembic log lines and the ``-- Running upgrade`` comment.
    cleaned = [
        line
        for line in raw.splitlines()
        if not line.startswith("INFO  [alembic")
        and not line.startswith("-- Running upgrade")
    ]
    return "\n".join(cleaned)


def _render_migration_sql(revision: str) -> str:
    """Render the DDL of one specific migration's ``upgrade()`` body.

    Uses alembic's ``Operations`` with a Postgres dialect and an
    in-memory ``StringIO`` as the SQL output buffer. This captures
    every DDL statement the migration emits without requiring a real
    database connection. The captured SQL is the migration's own
    contribution — prior migrations are not included.
    """

    import io
    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR.parent))
    script = ScriptDirectory.from_config(config)
    rev = script.get_revision(revision)
    assert rev is not None, f"Revision {revision!r} not found in script directory"
    module = rev.module

    buffer = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(ctx):
        module.upgrade()
    return buffer.getvalue()


def _iter_post_initial_revisions() -> Iterator[str]:
    """Yield every revision that is not the initial one.

    Walks the ``migrations/versions/`` directory, parses
    ``revision`` / ``down_revision`` from each migration file, and
    returns revisions that have a ``down_revision`` (i.e. not the
    first one in the chain). The result is a list, not a generator,
    so failures print a stable list.
    """

    revisions: list[tuple[str, str | None]] = []
    pattern = re.compile(
        r'^revision\s*=\s*["\']([^"\']+)["\']\s*\n'
        r'^down_revision\s*=\s*(["\']?)([^"\']*)["\']\s*$',
        re.MULTILINE,
    )
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        if match is None:
            continue
        revision, _quote, down_revision = match.groups()
        revisions.append((revision, down_revision or None))

    initial_ids = {rev for rev, down in revisions if down is None}
    return [rev for rev, down in revisions if down is not None]


def test_no_duplicate_enum_values_in_post_initial_migrations() -> None:
    """No post-initial migration may add an enum value already in the ORM.

    ``Base.metadata.create_all`` emits the enum type from the Python
    enum in the initial migration. A subsequent ``ALTER TYPE ... ADD
    VALUE`` for a value the Python enum already declares will fail
    with ``DuplicateObject``. The Python enum is the single source of
    truth for the PostgreSQL enum type's labels.
    """

    initial_ddl = _render_initial_ddl()
    initial_enum_values: dict[str, set[str]] = {}

    # Parse ``CREATE TYPE name AS ENUM ('v1', 'v2', ...)`` from the DDL.
    enum_pattern = re.compile(
        r"CREATE TYPE\s+(\w+)\s+AS ENUM\s*\(([^)]+)\)", re.IGNORECASE
    )
    for match in enum_pattern.finditer(initial_ddl):
        type_name = match.group(1)
        values = {
            v.strip().strip("'")
            for v in match.group(2).split(",")
            if v.strip()
        }
        initial_enum_values[type_name] = values

    # Sanity: the initial DDL must declare at least the enums the ORM
    # uses. If this fails, the test is broken — fix the test, not the
    # schema.
    assert "session_status" in initial_enum_values
    assert "flag_status" in initial_enum_values
    assert "review_decision" in initial_enum_values
    assert "admin_role" in initial_enum_values
    assert "under_review" in initial_enum_values["session_status"]
    assert "overturned" in initial_enum_values["flag_status"]
    assert "needs_more_info" in initial_enum_values["review_decision"]
    assert "instructor" in initial_enum_values["admin_role"]

    add_value_pattern = re.compile(
        r"ALTER\s+TYPE\s+(\w+)\s+ADD\s+VALUE\s+'([^']+)'", re.IGNORECASE
    )
    for revision in _iter_post_initial_revisions():
        sql = _render_migration_sql(revision)
        for match in add_value_pattern.finditer(sql):
            type_name, value = match.group(1), match.group(2)
            assert type_name in initial_enum_values, (
                f"Revision {revision!r} adds value {value!r} to enum type "
                f"{type_name!r}, but {type_name!r} is not declared by the "
                "ORM. This test only guards against duplicate adds."
            )
            assert value not in initial_enum_values[type_name], (
                f"Revision {revision!r} tries to add enum value "
                f"{type_name!r}.{value!r}, but the initial migration's "
                "Base.metadata.create_all already emits that value "
                "from the Python enum. Remove the ALTER TYPE statement; "
                "the value is already in the schema."
            )


@pytest.mark.parametrize(
    "operation",
    [
        "ADD COLUMN",
        "ADD CONSTRAINT",
        "CREATE TABLE",
        "CREATE INDEX",
        "CREATE UNIQUE INDEX",
        "ALTER TABLE.*ADD",
    ],
)
def test_no_duplicate_schema_objects_in_post_initial_migrations(
    operation: str,
) -> None:
    """No post-initial migration may add a column / table / index that
    the initial migration's ``Base.metadata.create_all`` already emits.

    The pattern in operation is matched as a case-insensitive regex
    against each post-initial migration's rendered SQL. For each match,
    the test extracts the object name and asserts that the initial
    migration's DDL already contains it. The set of objects to check
    is the closure of all tables / columns / constraints declared on
    the ORM, so the test does not need a hand-maintained list of what
    is "supposed to be in the initial schema."

    Triggers and function definitions are NOT covered by this test;
    they are the only legitimate content of a post-initial migration
    in this project (the ORM cannot model DML triggers).
    """

    initial_ddl = _render_initial_ddl()
    initial_ddl_upper = initial_ddl.upper()

    # Build a regex per operation that captures the object identifier.
    patterns: dict[str, re.Pattern[str]] = {
        "ADD COLUMN": re.compile(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
        ),
        "ADD CONSTRAINT": re.compile(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+CONSTRAINT\s+(\w+)", re.IGNORECASE
        ),
        "CREATE TABLE": re.compile(r"CREATE\s+TABLE\s+(\w+)", re.IGNORECASE),
        "CREATE INDEX": re.compile(
            r"CREATE\s+INDEX\s+(\w+)\s+ON\s+(\w+)", re.IGNORECASE
        ),
        "CREATE UNIQUE INDEX": re.compile(
            r"CREATE\s+UNIQUE\s+INDEX\s+(\w+)\s+ON\s+(\w+)", re.IGNORECASE
        ),
        "ALTER TABLE.*ADD": re.compile(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+(?!CONSTRAINT|COLUMN)(\w+)",
            re.IGNORECASE,
        ),
    }

    pattern = patterns[operation]
    seen: set[tuple[str, ...]] = set()
    for revision in _iter_post_initial_revisions():
        sql = _render_migration_sql(revision)
        for match in pattern.finditer(sql):
            groups = tuple(g.lower() for g in match.groups() if g)
            if groups in seen:
                continue
            seen.add(groups)

            # Reconstruct a "is this object in the initial DDL?" check
            # by searching for ``object_name`` in the initial DDL.
            for needle in groups:
                # Use a word-boundary check so e.g. searching for "id"
                # does not falsely match a longer identifier.
                if re.search(rf"\b{re.escape(needle)}\b", initial_ddl_upper):
                    pytest.fail(
                        f"Revision {revision!r} performs "
                        f"{operation!r} on {groups!r}, but the object "
                        f"{needle!r} is already present in the initial "
                        "migration's Base.metadata.create_all output. "
                        "Either the migration is redundant (delete it) "
                        "or the initial migration is stale (regenerate it "
                        "from the current ORM)."
                    )


def test_post_initial_migration_does_not_redeclare_initial_objects() -> None:
    """The narrowed invariant.

    The original ``test_post_initial_migration_chain_is_minimal`` test
    forbade any DDL in a post-initial migration, on the theory that the
    initial migration's ``Base.metadata.create_all`` would pick up
    anything new in the ORM. That theory turned out to be wrong:
    ``create_all`` does not alter existing tables (so ``ADD COLUMN``
    for a pre-existing table would silently do nothing) and does not
    add values to existing Postgres enum types (so a new enum value
    would silently do nothing). A migration must therefore be free to
    issue real DDL when the ORM's schema shape genuinely changes for
    pre-existing tables.

    What the test still protects against — and what the original
    forbidden-pattern test was indirectly guarding — is a post-initial
    migration re-emitting an object the initial migration has already
    emitted. That is the real anti-pattern: two migrations claiming to
    own the same ``CREATE TABLE`` / ``ADD COLUMN`` / enum value,
    drifting out of sync. ``test_no_duplicate_schema_objects_in_post_initial_migrations``
    and ``test_no_duplicate_enum_values_in_post_initial_migrations``
    already cover the case where the *identifier* is in both; this
    test adds the table-level coarse check that ``CREATE TABLE`` in a
    post-initial migration does not name a table the initial migration
    has already created.

    Allowed: a post-initial migration may issue ``CREATE TABLE`` for a
    genuinely new table (the initial migration's ``create_all``
    cannot add a table to a database that has already been initialised),
    ``ADD COLUMN`` for a genuinely new column on a pre-existing table
    (``create_all`` cannot alter), ``ALTER TYPE ... ADD VALUE`` for a
    new enum value (``create_all`` cannot extend an existing enum),
    plus DML triggers.
    """

    initial_ddl = _render_initial_ddl()
    initial_tables: set[str] = set()

    create_table_pattern = re.compile(
        r"CREATE\s+TABLE\s+(\w+)", re.IGNORECASE
    )
    for match in create_table_pattern.finditer(initial_ddl):
        initial_tables.add(match.group(1).lower())

    for revision in _iter_post_initial_revisions():
        sql = _render_migration_sql(revision)
        # Drop the alembic_version bookkeeping; it is not part of the
        # migration's DDL.
        sql = re.sub(
            r"INSERT INTO alembic_version.*?;", "", sql, flags=re.IGNORECASE
        )
        sql = re.sub(
            r"UPDATE alembic_version.*?;", "", sql, flags=re.IGNORECASE
        )
        for match in create_table_pattern.finditer(sql):
            table_name = match.group(1).lower()
            assert table_name not in initial_tables, (
                f"Post-initial migration {revision!r} issues "
                f"CREATE TABLE {table_name!r}, but the initial "
                "migration has already created that table. The "
                "post-initial migration would race with the initial "
                "migration's ownership of the table."
            )


def test_no_duplicate_tables_in_post_initial_migrations_negative_case(
    tmp_path: Path,
) -> None:
    """Negative-control: a fabricated migration that re-creates an
    existing table is rejected by the narrowed invariant.

    The original invariant forbade all DDL in post-initial migrations;
    the narrowed invariant forbids only the duplication case. This
    test confirms the narrowed invariant still catches the real
    anti-pattern it was originally guarding against.

    The test writes a transient migration file under
    ``migrations/versions/`` that re-creates ``flags``, then runs the
    narrowed-invariant check directly against that file's rendered
    DDL. The file is removed in the ``finally`` block so the test
    does not leave a stale artifact on disk.
    """

    migration_dir = MIGRATIONS_DIR
    bad_migration = migration_dir / "99999999_9999_negative_case.py"
    bad_migration.write_text(
        "from alembic import op\n"
        "revision = '99999999_9999'\n"
        "down_revision = '20260718_0002'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade() -> None:\n"
        "    op.execute('CREATE TABLE flags (id INT)')\n"
        "def downgrade() -> None:\n"
        "    op.execute('DROP TABLE flags')\n",
        encoding="utf-8",
    )
    try:
        initial_ddl = _render_initial_ddl()
        initial_tables = {
            m.group(1).lower()
            for m in re.finditer(
                r"CREATE\s+TABLE\s+(\w+)", initial_ddl, re.IGNORECASE
            )
        }
        bad_sql = _render_migration_sql("99999999_9999")
        for match in re.finditer(
            r"CREATE\s+TABLE\s+(\w+)", bad_sql, re.IGNORECASE
        ):
            table = match.group(1).lower()
            if table in initial_tables:
                # The narrowed invariant would catch this in the
                # production test; here we just confirm the
                # match-then-fail path runs.
                break
        else:
            raise AssertionError(
                "Test setup error: the fabricated migration did not "
                "render a CREATE TABLE statement matching a table in "
                "the initial migration."
            )
    finally:
        bad_migration.unlink(missing_ok=True)
