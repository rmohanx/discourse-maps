"""Export a space to an Obsidian vault as Discourse Graph-style notes.

One Markdown note per Source, titled with the plugin's Source format ("@{content}"),
with YAML frontmatter and wiki-links for relations. Notes are regenerated on every export,
so the database stays the source of truth (one-way sync for now).
"""

import re
from pathlib import Path

import psycopg
import yaml

from discourse_maps.config import DATA_DIR

DEFAULT_VAULT = DATA_DIR / "obsidian-vault"
GENERATED_MARK = "dm_generated: true"


def note_title(title: str) -> str:
    # Obsidian filenames cannot contain these characters.
    clean = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", title)
    return "@" + re.sub(r"\s+", " ", clean).strip()[:120]


def export_space(conn: psycopg.Connection, space_url: str, vault: Path = DEFAULT_VAULT) -> dict[str, int]:
    folder = vault / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    sources = conn.execute(
        """
        select k.id as concept_id, k.source_local_id, d.metadata, c.text,
               (select jsonb_object_agg(e.scheme, e.value) from external_identifier e
                 where e.entity_id = d.id) as ids
        from "Concept" k
        join "Space" s on s.id = k.space_id
        join "Concept" schema on schema.id = k.schema_id and schema.source_local_id = 'schema:node:source'
        join "Document" d on d.id = (k.reference_content->>'document')::bigint
        join "Content" c on c.id = (k.reference_content->>'content')::bigint
        where s.url = %s
        order by (d.metadata->>'year')::int, d.source_local_id
        """,
        (space_url,),
    ).fetchall()
    titles: dict[int, str] = {}
    for s in sources:
        title = note_title(s["metadata"].get("title", s["source_local_id"]))
        # Two papers can share a title; compare case-insensitively (macOS/Windows filenames).
        if title.casefold() in {t.casefold() for t in titles.values()}:
            title = f"{title} ({s['source_local_id'].removeprefix('source:')})"
        titles[s["concept_id"]] = title

    relations = conn.execute(
        """
        select r.name, (r.reference_content->>'source')::bigint as src,
               (r.reference_content->>'destination')::bigint as dst
        from "Concept" r join "Space" s on s.id = r.space_id
        where s.url = %s and r.is_relation and not r.is_schema
        """,
        (space_url,),
    ).fetchall()
    outgoing: dict[int, list[str]] = {}
    incoming: dict[int, list[str]] = {}
    for rel in relations:
        if rel["src"] in titles and rel["dst"] in titles:
            outgoing.setdefault(rel["src"], []).append(f"{rel['name']} [[{titles[rel['dst']]}]]")
            incoming.setdefault(rel["dst"], []).append(f"cited by [[{titles[rel['src']]}]]")

    # Remove stale generated notes, never hand-written ones.
    for old in folder.glob("*.md"):
        if GENERATED_MARK in old.read_text(encoding="utf-8")[:500]:
            old.unlink()

    for src in sources:
        meta, ids = src["metadata"], src["ids"] or {}
        openalex = meta.get("openalex") or {}
        front = {
            "nodeType": "Source",
            "dm_concept_id": src["concept_id"],
            "dm_generated": True,
            "authors": meta.get("authors", []),
            "year": meta.get("year"),
            "doi": ids.get("doi"),
            "openalex": ids.get("openalex"),
            "cited_by_count": openalex.get("cited_by_count"),
            "topics": openalex.get("topics"),
        }
        front = {k: v for k, v in front.items() if v not in (None, [], "")}
        text_parts = src["text"].split("\n\n", 1)
        body = [f"# {text_parts[0]}", ""]
        if len(text_parts) > 1:
            body += ["## Abstract", "", text_parts[1], ""]
        if ids.get("doi"):
            body += [f"DOI: https://doi.org/{ids['doi']}", ""]
        rels = outgoing.get(src["concept_id"], []) + incoming.get(src["concept_id"], [])
        if rels:
            body += ["## Relations", ""] + [f"- {r}" for r in sorted(rels)] + [""]
        note = (
            "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n" + "\n".join(body)
        )
        (folder / f"{titles[src['concept_id']]}.md").write_text(note, encoding="utf-8")
    return {"notes": len(sources), "relations": sum(len(v) for v in outgoing.values())}
