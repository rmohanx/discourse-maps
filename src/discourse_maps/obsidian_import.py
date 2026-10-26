"""Import a researcher's Obsidian vault (Discourse Graph plugin format) into the database.

Reads the same files the plugin writes:
  .obsidian/plugins/discourse-graphs/data.json   node types and relation types
  *.md with frontmatter nodeTypeId (+ nodeInstanceId once the plugin assigns one)
  relations.json                                 relation instances (vault root)
and maps them onto the core tables:
  note      -> Document (the file) + Content (title and body) + Concept (typed node)
  relation  -> Concept with is_relation, reference_content {source, destination}

Review state travels with each node: `status: candidate` and `proposed_by` (our convention for
LLM-proposed nodes) land in Concept.literal_content, and authorship goes to the proposing agent.
Re-running is idempotent; edited notes update in place.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import psycopg
import yaml
from psycopg.types.json import Jsonb

from discourse_maps import db
from discourse_maps.importer import (
    NODE_SCHEMAS,
    RELATION_SCHEMAS,
    ensure_schemas,
    find_same_work,
    upsert_returning,
)

PLUGIN_SETTINGS = Path(".obsidian/plugins/discourse-graphs/data.json")
NODE_KEY_BY_NAME = {name: key for key, name in NODE_SCHEMAS.items()}
RELATION_KEY_BY_LABEL = {label: key for key, label in RELATION_SCHEMAS.items()}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


@dataclass
class Note:
    path: str  # vault-relative
    title: str
    node_type: str  # Question / Claim / Evidence / Source
    instance_id: str | None
    frontmatter: dict
    body: str

    @property
    def key(self) -> str:
        # The plugin assigns nodeInstanceId lazily; until then the file path is the identity.
        return f"node:{self.instance_id}" if self.instance_id else f"path:{self.path}"


@dataclass
class Vault:
    root: Path
    notes: list[Note]
    relations: list[dict]  # {id, type_label, source_key, destination_key, ...}
    skipped: dict[str, int] = field(default_factory=dict)


def split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def read_vault(root: Path) -> Vault:
    settings = json.loads((root / PLUGIN_SETTINGS).read_text(encoding="utf-8"))
    node_types = {n["id"]: n["name"] for n in settings.get("nodeTypes", [])}
    relation_types = {r["id"]: r["label"] for r in settings.get("relationTypes", [])}

    notes, skipped = [], {"not_a_node": 0, "unknown_node_type": 0}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("."):
            continue
        front, body = split_frontmatter(path.read_text(encoding="utf-8"))
        type_id = front.get("nodeTypeId")
        if not type_id:
            skipped["not_a_node"] += 1
            continue
        if type_id not in node_types:
            skipped["unknown_node_type"] += 1
            continue
        notes.append(
            Note(rel, path.stem, node_types[type_id], front.get("nodeInstanceId"), front, body.strip())
        )

    by_instance = {n.instance_id: n for n in notes if n.instance_id}
    by_title = {n.title: n for n in notes}
    relations = []

    rel_file = root / "relations.json"
    if rel_file.exists():
        for inst in json.loads(rel_file.read_text(encoding="utf-8")).get("relations", {}).values():
            src, dst = by_instance.get(inst["source"]), by_instance.get(inst["destination"])
            label = relation_types.get(inst["type"])
            if src and dst and label:
                relations.append(
                    {**inst, "type_label": label, "source_key": src.key, "destination_key": dst.key}
                )
            else:
                skipped["dangling_relation"] = skipped.get("dangling_relation", 0) + 1

    # Older plugin versions stored relations in frontmatter: <relationTypeId>: "[[Target]]" or a list.
    for note in notes:
        for type_id, label in relation_types.items():
            raw = note.frontmatter.get(type_id)
            for link in raw if isinstance(raw, list) else [raw] if raw else []:
                target = _WIKILINK.search(str(link))
                if target and target.group(1) in by_title:
                    dst = by_title[target.group(1)]
                    relations.append(
                        {
                            "id": f"fm:{note.path}:{type_id}:{dst.path}",
                            "type_label": label,
                            "source_key": note.key,
                            "destination_key": dst.key,
                        }
                    )
    return Vault(root, notes, relations, skipped)


def _ensure_account(conn: psycopg.Connection, name: str, local_id: str, agent_type: str) -> int:
    row = conn.execute(
        """
        insert into "PlatformAccount" (name, platform, account_local_id, agent_type)
        values (%s, 'Obsidian', %s, %s)
        on conflict (platform, account_local_id) do update set name = excluded.name
        returning id
        """,
        (name, local_id, agent_type),
    ).fetchone()
    return row["id"]


def _rekey_path_identity(conn: psycopg.Connection, space_id: int, note: Note) -> None:
    """If the plugin has since given this note a nodeInstanceId, carry over the path-keyed rows."""
    if not note.instance_id:
        return
    old, new = f"path:{note.path}", note.key
    for table in ('"Document"', '"Content"', '"Concept"'):
        conn.execute(
            f"update {table} set source_local_id = %s where space_id = %s and source_local_id = %s",
            (new, space_id, old),
        )


def import_vault(conn: psycopg.Connection, root: Path) -> dict[str, int]:
    vault = read_vault(root)
    space_url = f"obsidian://vault/{quote(root.name)}"
    space_id = db.ensure_space(conn, space_url, f"Obsidian vault: {root.name}", platform="Obsidian")
    pipeline = db.ensure_agent(conn)
    owner = _ensure_account(conn, f"{root.name} vault owner", f"vault-owner:{root.name}", "person")

    with db.activity(conn, "import:obsidian", pipeline, {"vault": str(root), "space": space_url}) as act:
        for key, n in vault.skipped.items():
            act.count(f"skipped_{key}", n)
        schemas = ensure_schemas(conn, space_id, pipeline)
        concept_ids: dict[str, int] = {}

        for note in vault.notes:
            _rekey_path_identity(conn, space_id, note)
            proposer = note.frontmatter.get("proposed_by")
            author = (
                _ensure_account(conn, proposer, f"agent:{proposer}", "automated_agent") if proposer else owner
            )
            status = note.frontmatter.get("status", "accepted")  # hand-written notes count as accepted
            url = f"obsidian://open?vault={quote(root.name)}&file={quote(note.path)}"
            meta = {"path": note.path, "frontmatter": note.frontmatter}

            doc_id, inserted = upsert_returning(
                conn,
                """
                insert into "Document" (space_id, source_local_id, url, author_id, metadata, content_type)
                values (%s, %s, %s, %s, %s, 'text/markdown')
                on conflict (space_id, source_local_id) do update
                    set url = excluded.url, metadata = excluded.metadata, last_modified = now()
                returning id, (xmax = 0) as inserted
                """,
                (space_id, note.key, url, author, Jsonb(meta)),
            )
            act.record(doc_id, inserted)

            text = f"{note.title}\n\n{note.body}".strip()
            content_id, inserted = upsert_returning(
                conn,
                """
                insert into "Content" (document_id, space_id, source_local_id, text, scale, author_id,
                                       creator_id, content_type)
                values (%s, %s, %s, %s, 'document', %s, %s, 'text/markdown')
                on conflict (space_id, source_local_id, variant) do update
                    set text = excluded.text, document_id = excluded.document_id,
                        last_modified = case when "Content".text = excluded.text
                                             then "Content".last_modified else now() end
                returning id, (xmax = 0) as inserted
                """,
                (doc_id, space_id, note.key, text, author, owner),
            )
            act.record(content_id, inserted)
            # Text changed in this run (last_modified = this transaction's now()) -> embedding is stale.
            conn.execute(
                """
                update "ContentEmbedding_qwen3_embedding_0_6b_1024" e set obsolete = true
                from "Content" c where c.id = e.target_id and c.id = %s and c.last_modified = now()
                """,
                (content_id,),
            )

            literal = {"review_status": status, "proposed_by": proposer, "node_type": note.node_type}
            reference = {"document": doc_id, "content": content_id}
            if note.node_type == "Source":
                # Same paper already in another space (e.g. the Zitter import)? Point at it.
                same = find_same_work(
                    conn, space_id, note.frontmatter.get("doi"), note.frontmatter.get("zitter_key")
                )
                if same:
                    reference["same_work_as_document"] = same
            concept_id, inserted = upsert_returning(
                conn,
                """
                insert into "Concept" (name, space_id, author_id, schema_id, literal_content,
                                       reference_content, source_local_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set name = excluded.name, author_id = excluded.author_id, schema_id = excluded.schema_id,
                        literal_content = excluded.literal_content,
                        reference_content = excluded.reference_content, last_modified = now()
                returning id, (xmax = 0) as inserted
                """,
                (
                    note.title,
                    space_id,
                    author,
                    schemas[NODE_KEY_BY_NAME[note.node_type]],
                    Jsonb(literal),
                    Jsonb(reference),
                    note.key,
                ),
            )
            act.record(concept_id, inserted)
            act.count(f"{note.node_type.lower()}_{status}")
            concept_ids[note.key] = concept_id

        for rel in vault.relations:
            schema_key = RELATION_KEY_BY_LABEL.get(rel["type_label"])
            if not schema_key:
                act.count("skipped_unknown_relation_type")
                continue
            rel_id, inserted = upsert_returning(
                conn,
                """
                insert into "Concept" (name, space_id, author_id, schema_id, is_relation, arity,
                                       literal_content, reference_content, source_local_id)
                values (%s, %s, %s, %s, true, 2, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set reference_content = excluded.reference_content,
                        literal_content = excluded.literal_content
                returning id, (xmax = 0) as inserted
                """,
                (
                    rel["type_label"],
                    space_id,
                    owner,
                    schemas[schema_key],
                    Jsonb({"tentative": rel.get("tentative")}),
                    Jsonb(
                        {
                            "source": concept_ids[rel["source_key"]],
                            "destination": concept_ids[rel["destination_key"]],
                        }
                    ),
                    f"rel:{rel['id']}",
                ),
            )
            act.record(rel_id, inserted)
            act.count(f"relation_{schema_key}")
        act.count("notes", len(vault.notes))
    return act.summary
