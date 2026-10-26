"""The return trip: share nodes from the database into another researcher's Obsidian vault or Roam graph.

    dm share-to-obsidian --from roam://graph/X --vault ~/vault     Maya's Roam work appears in Sam's vault
    dm share-to-roam --from obsidian://vault/Y                      Sam's vault work appears in Maya's graph

Shared copies are mirrors, marked the way the Discourse Graph plugin marks imports: `importedFromRid`
points back at the original node, relations are written with `tentative: false` ("imported, not yet
accepted" in the plugin's convention). The importers recognise these copies and map them back to the
original Concept instead of creating duplicates, so relations the receiver adds to an imported node
attach to the original. Re-sharing refreshes the mirrors and never touches the receiver's own notes.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from discourse_maps.importer import make_rid, parse_rid
from discourse_maps.obsidian_import import PLUGIN_SETTINGS, split_frontmatter

RID_NAMESPACE = uuid.UUID("6f1c3a52-2d2a-4b8e-9a43-1b7d0c6e9a10")
OBSIDIAN_PREFIX = {"Question": "QUE", "Claim": "CLM", "Evidence": "EVD", "Source": "SRC"}
_TITLE_PREFIXES = re.compile(r"^(?:\[\[(QUE|CLM|EVD)\]\] - |(QUE|CLM|EVD|SRC) - |@)")


def stable_id(*parts: str) -> str:
    """Same input -> same id on every run, so re-sharing updates instead of duplicating."""
    return str(uuid.uuid5(RID_NAMESPACE, "|".join(parts)))


def bare_content(title: str) -> str:
    """'[[CLM]] - x', 'CLM - x', '@x' -> 'x'; also drops a Roam Evidence ' - @Source' suffix."""
    content = _TITLE_PREFIXES.sub("", title, count=1)
    return re.sub(r" - @.+$", "", content)


@dataclass
class SharedNode:
    key: str  # source_local_id in the origin space
    rid: str
    node_type: str
    content: str  # title without tool-specific prefix
    body: str
    review_status: str | None
    proposed_by: str | None


@dataclass
class SharedRelation:
    key: str
    label: str  # supports / opposes / informs / derived from / cites
    source: str  # node keys
    destination: str


def load_space(
    conn: psycopg.Connection, space_url: str, status: str = "all"
) -> tuple[list[SharedNode], list[SharedRelation]]:
    """Nodes (optionally only one review status) and the relations between them that originate here."""
    rows = conn.execute(
        """
        select k.id, k.name, k.source_local_id, k.literal_content, schema.name as type_name, c.text
        from "Concept" k
        join "Space" s on s.id = k.space_id
        join "Concept" schema on schema.id = k.schema_id
        left join "Content" c on c.id = (k.reference_content->>'content')::bigint
        where s.url = %s and not k.is_schema and not k.is_relation
          and not (k.literal_content ? 'imported_from_rid')
          and (%s = 'all' or coalesce(k.literal_content->>'review_status', 'accepted') = %s)
        """,
        (space_url, status, status),
    ).fetchall()
    nodes, key_by_id = [], {}
    for r in rows:
        text = r["text"] or r["name"]
        body = text.split("\n", 1)[1].strip() if "\n" in text else ""
        lit = r["literal_content"]
        nodes.append(
            SharedNode(
                r["source_local_id"],
                make_rid(space_url, r["source_local_id"]),
                r["type_name"],
                bare_content(r["name"]),
                body,
                lit.get("review_status"),
                lit.get("proposed_by"),
            )
        )
        key_by_id[r["id"]] = r["source_local_id"]
    relations = []
    for r in conn.execute(
        """
        select k.source_local_id, schema.name as label,
               (k.reference_content->>'source')::bigint as src, (k.reference_content->>'destination')::bigint as dst
        from "Concept" k join "Space" s on s.id = k.space_id join "Concept" schema on schema.id = k.schema_id
        where s.url = %s and k.is_relation and not k.is_schema
        """,
        (space_url,),
    ):
        if r["src"] in key_by_id and r["dst"] in key_by_id:
            relations.append(
                SharedRelation(r["source_local_id"], r["label"], key_by_id[r["src"]], key_by_id[r["dst"]])
            )
    return nodes, relations


# ---------------------------------------------------------------- to Obsidian


def _filename(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]', " ", text)).strip()[:150]


def share_to_obsidian(
    conn: psycopg.Connection, space_url: str, space_name: str, vault: Path, status: str = "all"
) -> dict[str, int]:
    settings = json.loads((vault / PLUGIN_SETTINGS).read_text(encoding="utf-8"))
    type_ids = {n["name"]: n["id"] for n in settings["nodeTypes"]}
    formats = {n["name"]: n.get("format") or f"{n['name']} - {{content}}" for n in settings["nodeTypes"]}
    relation_ids = {r["label"]: r["id"] for r in settings["relationTypes"]}
    nodes, relations = load_space(conn, space_url, status)
    folder = vault / "Shared" / _filename(space_name)
    folder.mkdir(parents=True, exist_ok=True)
    counts = {
        "notes_written": 0,
        "relations_written": 0,
        "skipped_unknown_type": 0,
        "skipped_relation_type": 0,
    }

    # Existing mirrors by rid, wherever the receiver moved them; their own notes are never touched.
    mirrors = {}
    for path in vault.rglob("*.md"):
        if path.relative_to(vault).as_posix().startswith("."):
            continue
        front, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        if parse_rid(str(front.get("importedFromRid", ""))):
            mirrors[front["importedFromRid"]] = path

    instance_ids = {}
    for node in nodes:
        if node.node_type not in type_ids:
            counts["skipped_unknown_type"] += 1
            continue
        instance_ids[node.key] = stable_id(vault.name, node.rid)
        title = formats[node.node_type].replace("{content}", node.content)
        front = {
            "nodeTypeId": type_ids[node.node_type],
            "nodeInstanceId": instance_ids[node.key],
            "importedFromRid": node.rid,
            "imported_from": space_name,
            "origin_review_status": node.review_status,
            "origin_proposed_by": node.proposed_by,
            "shared_at": time.strftime("%Y-%m-%d"),
        }
        head = "\n".join(
            f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v is not None
        )
        body = f"*Shared from **{space_name}**. Edits here are overwritten on the next share; link it, don't edit it.*\n\n{node.body}"
        path = mirrors.get(node.rid) or folder / f"{_filename(title)}.md"
        path.write_text(f"---\n{head}\n---\n\n{body.strip()}\n", encoding="utf-8")
        counts["notes_written"] += 1

    rel_path = vault / "relations.json"
    data = (
        json.loads(rel_path.read_text())
        if rel_path.exists()
        else {"version": 1, "lastModified": 0, "relations": {}}
    )
    now = int(time.time() * 1000)
    for rel in relations:
        if rel.label not in relation_ids:
            counts["skipped_relation_type"] += 1
            continue
        if rel.source not in instance_ids or rel.destination not in instance_ids:
            continue
        rid = stable_id(vault.name, space_url, rel.key)
        existing = data["relations"].get(rid, {})
        data["relations"][rid] = {
            "id": rid,
            "type": relation_ids[rel.label],
            "source": instance_ids[rel.source],
            "destination": instance_ids[rel.destination],
            "created": existing.get("created", now),
            "lastModified": now,
            "importedFromRid": make_rid(space_url, rel.key),
            "tentative": False,  # plugin convention: imported, not yet accepted
        }
        counts["relations_written"] += 1
    data["lastModified"] = now
    rel_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return counts


# ---------------------------------------------------------------- to Roam

ROAM_RELATION_MARKERS = {"supports": "SupportedBy", "opposes": "OpposedBy"}


def roam_title(node: SharedNode, source_titles: dict[str, str], derived: dict[str, str]) -> str:
    if node.node_type == "Source":
        return f"@{node.content}"
    tag = {"Question": "QUE", "Claim": "CLM", "Evidence": "EVD"}[node.node_type]
    title = f"[[{tag}]] - {node.content}"
    if node.node_type == "Evidence" and node.key in derived:  # Roam encodes "derived from" in the title
        title += f" - {source_titles[derived[node.key]]}"
    return title


def share_to_roam(
    conn: psycopg.Connection, space_url: str, space_name: str, client, status: str = "all"
) -> dict[str, int]:
    from discourse_maps import roam

    nodes, relations = load_space(conn, space_url, status)
    by_key = {n.key: n for n in nodes}
    derived = {
        r.source: r.destination
        for r in relations
        if r.label == "derived from"
        and by_key.get(r.destination)
        and by_key[r.destination].node_type == "Source"
    }
    source_titles = {n.key: f"@{n.content}" for n in nodes if n.node_type == "Source"}
    titles = {n.key: roam_title(n, source_titles, derived) for n in nodes if n.node_type in OBSIDIAN_PREFIX}

    graph = roam.read_graph_api(client)
    uid_by_title = {t: u for u, t in graph.pages.items()}
    existing_rels = {
        (r, graph.pages.get(s), graph.pages.get(d))
        for r, s, d, _ in roam.infer_relations(graph, roam.classify_pages(graph))
    }
    counts = {"pages_created": 0, "pages_existing": 0, "relations_added": 0, "relations_existing": 0}

    def write(body: dict) -> None:
        url = f"{client.base}/write"
        for _ in range(8):
            resp = client.http.post(url, headers=client.headers, json=body)
            if resp.status_code == 429:  # Roam rate limit: wait and retry
                time.sleep(int(resp.headers.get("retry-after", 10)))
                continue
            if resp.status_code in (307, 308):
                url = resp.headers["location"]
                client.base = url.rsplit("/", 1)[0]
                continue
            resp.raise_for_status()
            return
        raise RuntimeError("Roam API kept redirecting")

    def block(parent: str, text: str, order: int | str = "last", seed: str = "") -> str:
        uid = stable_id("roam-block", parent, text, seed)[:9].replace("-", "x")
        write(
            {
                "action": "create-block",
                "location": {"parent-uid": parent, "order": order},
                "block": {"string": text, "uid": uid},
            }
        )
        return uid

    for key, title in titles.items():
        node = by_key[key]
        if title in uid_by_title and graph.children(uid_by_title[title]):
            counts["pages_existing"] += 1  # mirror exists (or the receiver wrote it): leave it alone
            continue
        if title in uid_by_title:
            page = uid_by_title[title]
        else:
            page = stable_id("roam-page", node.rid)[:9].replace("-", "x")
            write({"action": "create-page", "page": {"title": title, "uid": page}})
            uid_by_title[title] = page
        block(page, f"imported_from:: {space_name}")
        block(page, f"imported_from_rid:: {node.rid}")
        if node.review_status:
            block(page, f"origin_review_status:: {node.review_status}")
        for line in [ln for ln in node.body.splitlines() if ln.strip()][:20]:
            block(page, line.strip()[:2000])
        counts["pages_created"] += 1

    roam_label = {"supports": "supports", "opposes": "opposes", "informs": "informs"}
    for rel in relations:
        if rel.label not in roam_label or rel.source not in titles or rel.destination not in titles:
            continue
        src, dst = titles[rel.source], titles[rel.destination]
        if (rel.label, src, dst) in existing_rels:
            counts["relations_existing"] += 1
            continue
        if rel.label == "informs":  # a block on the Question page linking the Evidence
            block(uid_by_title[dst], f"[[{src}]]")
        else:  # Claim page: [[SupportedBy]] / [[OpposedBy]] block with the Evidence nested under it
            marker = block(uid_by_title[dst], f"[[{ROAM_RELATION_MARKERS[rel.label]}]]", seed=rel.key)
            block(marker, f"[[{src}]]")
        counts["relations_added"] += 1
    return counts
