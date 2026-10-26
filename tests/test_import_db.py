"""Integration tests against the local Supabase database (`supabase start`)."""

from pathlib import Path

import psycopg
import pytest

from discourse_maps.bibtex import parse_file
from discourse_maps.db import connect
from discourse_maps.importer import import_records

pytestmark = pytest.mark.db

TEST_SPACE = ("test://discourse-maps/import", "Import test space")
FIXTURE = Path(__file__).parent / "fixtures" / "sample.bib"


@pytest.fixture
def conn():
    try:
        conn = connect()
    except psycopg.OperationalError:
        pytest.skip("local database not running (supabase start)")
    yield conn
    conn.rollback()
    conn.execute('delete from "Space" where url = %s', (TEST_SPACE[0],))
    conn.commit()
    conn.close()


def _counts(conn) -> dict:
    return conn.execute(
        """
        select count(distinct d.id) as documents, count(distinct c.id) as contents,
               count(distinct k.id) as concepts
        from "Space" s
        join "Document" d on d.space_id = s.id
        join "Content" c on c.document_id = d.id
        join "Concept" k on k.space_id = s.id and k.source_local_id like 'source:%%'
        where s.url = %s
        """,
        (TEST_SPACE[0],),
    ).fetchone()


def test_import_is_idempotent(conn):
    records = parse_file(FIXTURE)
    first = import_records(conn, records, TEST_SPACE)
    assert first["generated"] == 9  # 3 records x (Document, Content, Concept)
    second = import_records(conn, records, TEST_SPACE)
    assert "generated" not in second
    assert second["existing"] == 9
    assert _counts(conn) == {"documents": 3, "contents": 3, "concepts": 3}


def test_import_records_provenance(conn):
    import_records(conn, parse_file(FIXTURE), TEST_SPACE)
    row = conn.execute(
        """
        select a.activity_type, a.status, count(g.*) as generated
        from prov_activity a join prov_generation g on g.activity_id = a.id
        where a.parameters->>'source' = %s
        group by a.id order by a.id desc limit 1
        """,
        (TEST_SPACE[0],),
    ).fetchone()
    assert row == {"activity_type": "import:bibtex", "status": "complete", "generated": 9}


def test_has_permission_denies_without_role(conn):
    import_records(conn, parse_file(FIXTURE), TEST_SPACE)
    space_id = conn.execute('select id from "Space" where url = %s', (TEST_SPACE[0],)).fetchone()["id"]
    # No auth.uid() in a direct connection, so no account matches: access is denied.
    allowed = conn.execute("select public.has_permission(%s, 'content:read') as ok", (space_id,)).fetchone()
    assert allowed["ok"] is False
