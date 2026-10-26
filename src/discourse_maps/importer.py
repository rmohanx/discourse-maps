"""Import BibTeX records as discourse-graph Sources.

Each record becomes:
  Document  - the paper (bibliographic metadata in `metadata`)
  Content   - its searchable text (title, later + abstract), scale 'document'
  Concept   - a Source node instance, typed by the space's "Source" node schema
plus external identifiers (bibtex key, DOI, URL) and PROV-O generation records.

Every write is an upsert on a natural key, so re-running an import changes nothing new.
"""

from collections.abc import Iterable

import psycopg
from psycopg.types.json import Jsonb

from discourse_maps import db
from discourse_maps.bibtex import BibRecord

ZITTER_SPACE = ("https://www.zitter-institute.org/p/zitter-repository.html", "Zitter Repository")

# Node and relation schemas, named as in the Discourse Graphs Obsidian plugin defaults.
NODE_SCHEMAS = {"source": "Source", "claim": "Claim", "evidence": "Evidence", "question": "Question"}
RELATION_SCHEMAS = {
    "supports": "supports",
    "opposes": "opposes",
    "informs": "informs",
    "derived_from": "derived from",
    "cites": "cites",  # ours: bibliographic citation between Sources (from OpenAlex)
}


def upsert_returning(conn: psycopg.Connection, sql: str, params: tuple) -> tuple[int, bool]:
    """Run an INSERT ... ON CONFLICT DO UPDATE ... RETURNING id, (xmax = 0) AS inserted."""
    row = conn.execute(sql, params).fetchone()
    return row["id"], row["inserted"]


def ensure_schemas(conn: psycopg.Connection, space_id: int, agent_id: int) -> dict[str, int]:
    ids = {}
    for key, name in NODE_SCHEMAS.items():
        ids[key], _ = upsert_returning(
            conn,
            """
            insert into "Concept" (name, space_id, author_id, is_schema, source_local_id)
            values (%s, %s, %s, true, %s)
            on conflict (space_id, source_local_id) do update set name = excluded.name
            returning id, (xmax = 0) as inserted
            """,
            (name, space_id, agent_id, f"schema:node:{key}"),
        )
    for key, label in RELATION_SCHEMAS.items():
        ids[key], _ = upsert_returning(
            conn,
            """
            insert into "Concept" (name, space_id, author_id, is_schema, is_relation, arity, source_local_id)
            values (%s, %s, %s, true, true, 2, %s)
            on conflict (space_id, source_local_id) do update set name = excluded.name
            returning id, (xmax = 0) as inserted
            """,
            (label, space_id, agent_id, f"schema:relation:{key}"),
        )
    return ids


def add_identifier(conn: psycopg.Connection, entity_id: int, scheme: str, value: str | None) -> None:
    if value:
        conn.execute(
            """
            insert into external_identifier (entity_id, scheme, value) values (%s, %s, %s)
            on conflict do nothing
            """,
            (entity_id, scheme, value),
        )


def import_records(
    conn: psycopg.Connection, records: Iterable[BibRecord], space: tuple[str, str] = ZITTER_SPACE
) -> dict[str, int]:
    """Import records into `space` (url, name)."""
    records = list(records)
    space_id = db.ensure_space(conn, *space, platform="Bibtex")
    agent_id = db.ensure_agent(conn)
    params = {"source": space[0], "files": sorted({r.source_file for r in records})}
    with db.activity(conn, "import:bibtex", agent_id, params) as act:
        schemas = ensure_schemas(conn, space_id, agent_id)
        for rec in records:
            meta = {
                "bibtex": {"type": rec.entry_type, "file": rec.source_file, **rec.fields},
                "title": rec.title,
                "authors": rec.authors,
                "year": rec.year,
            }
            doc_id, inserted = upsert_returning(
                conn,
                """
                insert into "Document" (space_id, source_local_id, url, author_id, metadata)
                values (%s, %s, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set url = excluded.url,
                        metadata = "Document".metadata || excluded.metadata,
                        last_modified = case when "Document".metadata @> excluded.metadata
                                             then "Document".last_modified else now() end
                returning id, (xmax = 0) as inserted
                """,
                (space_id, rec.key, rec.url, agent_id, Jsonb(meta)),
            )
            act.record(doc_id, inserted)

            # Text is only seeded here; enrichment may append an abstract, so never overwrite it.
            content_id, inserted = upsert_returning(
                conn,
                """
                insert into "Content" (document_id, space_id, source_local_id, text, scale,
                                       author_id, creator_id)
                values (%s, %s, %s, %s, 'document', %s, %s)
                on conflict (space_id, source_local_id, variant) do update
                    set document_id = excluded.document_id
                returning id, (xmax = 0) as inserted
                """,
                (doc_id, space_id, rec.key, rec.title, agent_id, agent_id),
            )
            act.record(content_id, inserted)

            concept_id, inserted = upsert_returning(
                conn,
                """
                insert into "Concept" (name, space_id, author_id, schema_id, literal_content,
                                       reference_content, source_local_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set name = excluded.name,
                        literal_content = excluded.literal_content,
                        reference_content = excluded.reference_content
                returning id, (xmax = 0) as inserted
                """,
                (
                    rec.title,
                    space_id,
                    agent_id,
                    schemas["source"],
                    Jsonb({"authors": rec.authors, "year": rec.year}),
                    Jsonb({"document": doc_id, "content": content_id}),
                    f"source:{rec.key}",
                ),
            )
            act.record(concept_id, inserted)

            add_identifier(conn, doc_id, "bibtex_key", f"{space_id}:{rec.key}")
            # A DOI or URL identifies one work globally; the first space to import it keeps it.
            add_identifier(conn, doc_id, "doi", rec.doi)
            add_identifier(conn, doc_id, "url", rec.url)
        act.count("records", len(records))
    return act.summary


def find_same_work(
    conn: psycopg.Connection, space_id: int, doi: str | None, bib_key: str | None
) -> int | None:
    """Document id of the same paper in another space (by DOI, else BibTeX key): one copy per work."""
    if not (doi or bib_key):
        return None
    row = conn.execute(
        """
        select e.entity_id from external_identifier e join "Document" d on d.id = e.entity_id
        where d.space_id <> %(space)s
          and ((e.scheme = 'doi' and e.value = %(doi)s)
               or (e.scheme = 'bibtex_key' and e.value = d.space_id || ':' || %(key)s))
        order by e.scheme = 'doi' desc limit 1
        """,
        {"space": space_id, "doi": str(doi).lower() if doi else None, "key": bib_key},
    ).fetchone()
    return row["entity_id"] if row else None
