"""The return trip: share nodes from the database into another researcher's Obsidian vault or Roam graph.

    dm share-to-obsidian --from roam://graph/X --vault ~/vault     Maya's Roam work appears in Sam's vault
    dm share-to-roam --from obsidian://vault/Y                      Sam's vault work appears in Maya's graph

Shared copies are mirrors of an original owned by another space, marked the way the Discourse Graph
plugin marks imports (`importedFromRid`, relations with `tentative: false`). Sync state per
(target, original) lives in `share_record`; history lives in `entity_revision`. The scenarios
(first share, refresh, receiver edits, deletions) and their policies are in docs/sync.md:
mirrors show the latest version with a banner pointing at the database history, receiver edits are
never overwritten (the incoming version goes beside them and a conflict is recorded), and deletions
are respected but never propagated destructively.
"""

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import psycopg

from discourse_maps import db
from discourse_maps.importer import make_rid, parse_rid
from discourse_maps.obsidian_import import PLUGIN_SETTINGS, split_frontmatter

RID_NAMESPACE = uuid.UUID("6f1c3a52-2d2a-4b8e-9a43-1b7d0c6e9a10")
ROAM_TAGS = {"Question": "QUE", "Claim": "CLM", "Evidence": "EVD"}
_TITLE_PREFIXES = re.compile(r"^(?:\[\[(QUE|CLM|EVD)\]\] - |(QUE|CLM|EVD|SRC|HYP) - |@)")


def stable_id(*parts: str) -> str:
    """Same input -> same id on every run, so re-sharing updates instead of duplicating."""
    return str(uuid.uuid5(RID_NAMESPACE, "|".join(parts)))


def body_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


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
    concept_id: int = 0
    version: int = 1
    deleted: bool = False

    @property
    def origin_hash(self) -> str:
        return body_hash(f"{self.node_type}\n{self.content}\n{self.review_status}\n{self.body}")


@dataclass
class SharedRelation:
    key: str
    label: str  # supports / opposes / informs / derived from / cites / custom
    source: str  # node keys
    destination: str
    deleted: bool = False


def load_space(
    conn: psycopg.Connection, space_url: str, status: str = "all"
) -> tuple[list[SharedNode], list[SharedRelation]]:
    """Nodes that originate in this space (deleted ones included, so their mirrors can be flagged)."""
    rows = conn.execute(
        """
        select k.id, k.name, k.source_local_id, k.literal_content, schema.name as type_name, c.text,
               coalesce(public.content_version(c.id), 1) as version
        from "Concept" k
        join "Space" s on s.id = k.space_id
        join "Concept" schema on schema.id = k.schema_id
        left join "Content" c on c.id = (k.reference_content->>'content')::bigint
        where s.url = %s and not k.is_schema and not k.is_relation
          and (%s = 'all' or k.literal_content ? 'deleted_at'
               or coalesce(k.literal_content->>'review_status', 'accepted') = %s)
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
                r["id"],
                r["version"],
                "deleted_at" in lit,
            )
        )
        key_by_id[r["id"]] = r["source_local_id"]
    relations = []
    for r in conn.execute(
        """
        select k.source_local_id, schema.name as label, k.literal_content ? 'deleted_at' as deleted,
               (k.reference_content->>'source')::bigint as src, (k.reference_content->>'destination')::bigint as dst
        from "Concept" k join "Space" s on s.id = k.space_id join "Concept" schema on schema.id = k.schema_id
        where s.url = %s and k.is_relation and not k.is_schema
        """,
        (space_url,),
    ):
        if r["src"] in key_by_id and r["dst"] in key_by_id:
            relations.append(
                SharedRelation(
                    r["source_local_id"], r["label"], key_by_id[r["src"]], key_by_id[r["dst"]], r["deleted"]
                )
            )
    return nodes, relations


# ---------------------------------------------------------------- sync ledger (share_record / sync_conflict)


def get_record(conn: psycopg.Connection, target: str, concept_id: int) -> dict | None:
    return conn.execute(
        "select * from share_record where target = %s and origin_concept_id = %s", (target, concept_id)
    ).fetchone()


def put_record(
    conn, act: db.Activity, target: str, node: SharedNode, mirror_ref: str, shared_hash: str, status: str
):
    conn.execute(
        """
        insert into share_record (target, origin_concept_id, mirror_ref, shared_hash, origin_hash,
                                  origin_version, status, activity_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (target, origin_concept_id) do update
            set mirror_ref = excluded.mirror_ref, shared_hash = excluded.shared_hash,
                origin_hash = excluded.origin_hash, origin_version = excluded.origin_version,
                status = excluded.status, shared_at = now(), activity_id = excluded.activity_id
        """,
        (target, node.concept_id, mirror_ref, shared_hash, node.origin_hash, node.version, status, act.id),
    )


def set_status(conn, act: db.Activity, target: str, node: SharedNode, status: str) -> None:
    conn.execute(
        """update share_record set status = %s, activity_id = %s
           where target = %s and origin_concept_id = %s and status <> %s""",
        (status, act.id, target, node.concept_id, status),
    )


def record_conflict(
    conn, act: db.Activity, target: str, node: SharedNode, mirror_ref: str, incoming_ref: str
):
    conn.execute(
        """insert into sync_conflict (target, origin_concept_id, mirror_ref, incoming_ref, activity_id)
           values (%s, %s, %s, %s, %s)""",
        (target, node.concept_id, mirror_ref, incoming_ref, act.id),
    )


def classify(record: dict | None, node: SharedNode, mirror_exists: bool, current_hash: str | None) -> str:
    """Decide the scenario for one node (docs/sync.md S1-S6)."""
    if record and not mirror_exists:
        return "deleted_by_receiver"  # S5
    if node.deleted:
        return "origin_deleted" if mirror_exists else "skip"  # S6
    if not mirror_exists:
        return "create"  # S1
    edited = record is not None and current_hash != record["shared_hash"]
    origin_changed = record is None or record["origin_hash"] != node.origin_hash
    if edited and origin_changed:
        return "conflict"  # S4
    if edited:
        return "edited_no_update"
    return "refresh" if origin_changed else "unchanged"  # S3 / S2


# ---------------------------------------------------------------- to Obsidian


def _filename(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]', " ", text)).strip()[:150]


def _banner(space_name: str, node: SharedNode, today: str) -> str:
    return (
        f"> [!info] Shared copy of **{space_name}**, latest version **v{node.version}** ({today}). "
        f'Full history is in the database: `dm history "{node.rid}"`. Edits here are not synced back; '
        f"to contribute, create your own node and relate it."
    )


def _write_note(at: Path, front: dict, body: str) -> str:
    """Write a note; return the hash of its body as written (for edit detection)."""
    head = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v is not None)
    at.parent.mkdir(parents=True, exist_ok=True)
    at.write_text(f"---\n{head}\n---\n\n{body.strip()}\n", encoding="utf-8")
    return body_hash(split_frontmatter(at.read_text(encoding="utf-8"))[1])


def share_to_obsidian(
    conn: psycopg.Connection, space_url: str, space_name: str, vault: Path, status: str = "all"
) -> dict[str, int]:
    settings = json.loads((vault / PLUGIN_SETTINGS).read_text(encoding="utf-8"))
    type_ids = {n["name"]: n["id"] for n in settings["nodeTypes"]}
    formats = {n["name"]: n.get("format") or f"{n['name']} - {{content}}" for n in settings["nodeTypes"]}
    relation_ids = {r["label"]: r["id"] for r in settings["relationTypes"]}
    target = f"obsidian://vault/{quote(vault.name)}"
    nodes, relations = load_space(conn, space_url, status)
    folder = vault / "Shared" / _filename(space_name)
    today = time.strftime("%Y-%m-%d")

    # Mirrors found by rid, wherever the receiver moved them; their own notes are never touched.
    mirrors: dict[str, Path] = {}
    for path in vault.rglob("*.md"):
        if path.relative_to(vault).as_posix().startswith("."):
            continue
        front, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        if parse_rid(front.get("importedFromRid")):
            mirrors[str(front["importedFromRid"])] = path

    agent = db.ensure_agent(conn)
    with db.activity(
        conn, "share:obsidian", agent, {"from": space_url, "to": target, "status": status}
    ) as act:
        instance_ids = {}
        for node in nodes:
            if node.node_type not in type_ids:
                act.count("skipped_unknown_type")
                continue
            instance_ids[node.key] = stable_id(vault.name, node.rid)
            record = get_record(conn, target, node.concept_id)
            path = mirrors.get(node.rid)
            rel_path = lambda p: p.relative_to(vault).as_posix()  # noqa: E731
            current = body_hash(split_frontmatter(path.read_text(encoding="utf-8"))[1]) if path else None
            action = classify(record, node, path is not None, current)
            act.count(action)

            if action in ("skip", "unchanged", "edited_no_update"):
                if record is None and path:  # adopt a mirror written before the ledger existed
                    put_record(conn, act, target, node, rel_path(path), current, "active")
                continue
            if action == "deleted_by_receiver":
                set_status(conn, act, target, node, "deleted_by_receiver")
                continue
            if action == "origin_deleted":
                fm, kept = split_frontmatter(path.read_text(encoding="utf-8"))
                _write_note(path, {**fm, "dm_status": "origin_deleted"}, kept)  # flag only; delete nothing
                set_status(conn, act, target, node, "origin_deleted")
                continue

            title = formats[node.node_type].replace("{content}", node.content)
            front = {
                "nodeTypeId": type_ids[node.node_type],
                "nodeInstanceId": instance_ids[node.key],
                "importedFromRid": node.rid,
                "imported_from": space_name,
                "origin_review_status": node.review_status,
                "origin_proposed_by": node.proposed_by,
                "dm_version": node.version,
                "dm_synced_at": today,
                "dm_status": "latest",
            }
            if action == "conflict":
                # Keep the receiver's edit; put the incoming version beside it as a plain (non-node) note.
                incoming = folder / "_conflicts" / f"{_filename(title)} (incoming v{node.version}).md"
                _write_note(
                    incoming,
                    {"dm_conflict_for": node.rid, "dm_version": node.version, "dm_synced_at": today},
                    f"> [!warning] The original changed after this copy was edited locally. Your edited copy is "
                    f"unchanged at `{rel_path(path)}`; this is the incoming version.\n\n{node.body}",
                )
                fm, kept = split_frontmatter(path.read_text(encoding="utf-8"))
                _write_note(
                    path, {**fm, "dm_status": "conflict", "dm_conflict_file": rel_path(incoming)}, kept
                )
                record_conflict(conn, act, target, node, rel_path(path), rel_path(incoming))
                put_record(conn, act, target, node, rel_path(path), record["shared_hash"], "conflict")
                continue

            new_path = folder / f"{_filename(title)}.md"
            if path is not None and path != new_path:
                if path.parent == folder:
                    path.unlink()  # our own copy under its old title: rename by rewriting
                else:
                    new_path = path  # the receiver moved it: refresh in place
            written = _write_note(new_path, front, f"{_banner(space_name, node, today)}\n\n{node.body}")
            put_record(conn, act, target, node, rel_path(new_path), written, "active")

        # Relations: shared ones are ours to add, refresh, or withdraw; the receiver's are never touched.
        rel_file = vault / "relations.json"
        data = (
            json.loads(rel_file.read_text())
            if rel_file.exists()
            else {"version": 1, "lastModified": 0, "relations": {}}
        )
        now = int(time.time() * 1000)
        for rel in relations:
            rid = stable_id(vault.name, space_url, rel.key)
            if rel.deleted:
                if data["relations"].pop(rid, None):
                    act.count("relations_withdrawn")
                continue
            if rel.label not in relation_ids:
                act.count("skipped_relation_type")
                continue
            if rel.source not in instance_ids or rel.destination not in instance_ids:
                continue
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
            act.count("relations_written")
        data["lastModified"] = now
        rel_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return act.summary


# ---------------------------------------------------------------- to Roam

ROAM_RELATION_MARKERS = {"supports": "SupportedBy", "opposes": "OpposedBy"}
_ATTRIBUTE_LINE = re.compile(r"^[\w -]+::")
_REF_ONLY = re.compile(r"^\[\[.+\]\]$")


class RoamTarget(Protocol):
    """What sharing needs from a Roam graph; RoamApiTarget talks to the live API, tests use a fake."""

    name: str

    def read(self): ...
    def create_page(self, title: str, uid: str) -> None: ...
    def update_page(self, uid: str, title: str) -> None: ...
    def create_block(self, parent: str, text: str, uid: str, order: int | str = "last") -> None: ...
    def update_block(self, uid: str, text: str) -> None: ...
    def delete_block(self, uid: str) -> None: ...


class RoamApiTarget:
    def __init__(self, client):
        self.client, self.name = client, client.graph

    def read(self):
        from discourse_maps import roam

        return roam.read_graph_api(self.client)

    def _write(self, body: dict) -> None:
        url = f"{self.client.base}/write"
        for _ in range(8):
            resp = self.client.http.post(url, headers=self.client.headers, json=body)
            if resp.status_code == 429:  # rate limited
                time.sleep(int(resp.headers.get("retry-after", 10)))
                continue
            if resp.status_code in (307, 308):
                url = resp.headers["location"]
                self.client.base = url.rsplit("/", 1)[0]
                continue
            resp.raise_for_status()
            return
        raise RuntimeError("Roam API kept redirecting or rate limiting")

    def create_page(self, title, uid):
        self._write({"action": "create-page", "page": {"title": title, "uid": uid}})

    def update_page(self, uid, title):
        self._write({"action": "update-page", "page": {"uid": uid, "title": title}})

    def create_block(self, parent, text, uid, order="last"):
        self._write(
            {
                "action": "create-block",
                "location": {"parent-uid": parent, "order": order},
                "block": {"string": text, "uid": uid},
            }
        )

    def update_block(self, uid, text):
        self._write({"action": "update-block", "block": {"uid": uid, "string": text}})

    def delete_block(self, uid):
        self._write({"action": "delete-block", "block": {"uid": uid}})


def roam_title(node: SharedNode, source_titles: dict[str, str], derived: dict[str, str]) -> str:
    if node.node_type == "Source":
        return f"@{node.content}"
    title = f"[[{ROAM_TAGS[node.node_type]}]] - {node.content}"
    if node.node_type == "Evidence" and node.key in derived:  # Roam encodes "derived from" in the title
        title += f" - {source_titles[derived[node.key]]}"
    return title


def _is_content_line(text: str) -> bool:
    """Mirror content = not an attribute (dm_*, imported_from::, ...) and not a relation/ref-only block."""
    return not _ATTRIBUTE_LINE.match(text) and not _REF_ONLY.match(text)


def roam_body_blocks(graph, page_uid: str) -> list[str]:
    return [u for u in graph.children(page_uid) if _is_content_line(graph.blocks[u]["string"])]


def _roam_uid(*parts: str) -> str:
    return stable_id(*parts).replace("-", "")[:9]


def share_to_roam(
    conn: psycopg.Connection, space_url: str, space_name: str, roam_target, status: str = "all"
) -> dict[str, int]:
    from discourse_maps import roam

    if not hasattr(roam_target, "read"):  # accept a raw RoamClient too
        roam_target = RoamApiTarget(roam_target)
    target = f"roam://graph/{roam_target.name}"
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
    sharable = [n for n in nodes if n.node_type in ROAM_TAGS or n.node_type == "Source"]
    titles = {n.key: roam_title(n, source_titles, derived) for n in sharable}
    graph = roam_target.read()
    uid_by_title = {t: u for u, t in graph.pages.items()}
    today = time.strftime("%Y-%m-%d")

    def attr_blocks(page: str) -> dict[str, str]:
        found = {}
        for uid in graph.children(page):
            text = graph.blocks[uid]["string"]
            if "::" in text:
                found[text.split("::", 1)[0].strip()] = uid
        return found

    def set_attr(page: str, key: str, value: str, existing: dict[str, str]) -> None:
        text = f"{key}:: {value}"
        if key not in existing:
            roam_target.create_block(page, text, _roam_uid("attr", page, key), "last")
        elif graph.blocks[existing[key]]["string"] != text:
            roam_target.update_block(existing[key], text)

    def write_mirror(page: str, node: SharedNode) -> str:
        existing = attr_blocks(page)
        set_attr(
            page,
            "dm_mirror",
            f"shared copy of {space_name}, latest version v{node.version} ({today}); "
            "full history in the database (dm history)",
            existing,
        )
        set_attr(page, "imported_from", space_name, existing)
        set_attr(page, "imported_from_rid", node.rid, existing)
        if node.review_status:
            set_attr(page, "origin_review_status", node.review_status, existing)
        set_attr(page, "dm_version", str(node.version), existing)
        set_attr(page, "dm_synced_at", today, existing)
        set_attr(page, "dm_status", "latest", existing)
        for uid in roam_body_blocks(graph, page):
            roam_target.delete_block(uid)
        lines = [ln.strip()[:2000] for ln in node.body.splitlines() if ln.strip()][:50]
        content = [ln for ln in lines if _is_content_line(ln)]
        for i, line in enumerate(content):
            roam_target.create_block(page, line, _roam_uid("body", page, str(node.version), str(i)), "last")
        return body_hash("\n".join(content))

    agent = db.ensure_agent(conn)
    with db.activity(conn, "share:roam", agent, {"from": space_url, "to": target, "status": status}) as act:
        for node in sharable:
            title = titles[node.key]
            record = get_record(conn, target, node.concept_id)
            page = record["mirror_ref"] if record else None
            if page is None and title in uid_by_title:
                candidate = uid_by_title[title]
                if roam.page_attributes(graph, candidate).get("imported_from_rid") == node.rid:
                    page = candidate  # adopt a mirror written before the ledger existed
                elif graph.children(candidate):
                    act.count("skipped_title_taken")  # the receiver has their own page with this title
                    continue
                else:
                    page = candidate  # empty page auto-created by a link: fill it
            exists = page is not None and page in graph.pages
            current = None
            if exists and record:
                current = body_hash(
                    "\n".join(graph.blocks[u]["string"] for u in roam_body_blocks(graph, page))
                )
            # An adopted or empty page with no ledger entry yet: write our content into it.
            action = "create" if exists and record is None else classify(record, node, exists, current)
            act.count(action)

            if action in ("skip", "unchanged", "edited_no_update"):
                continue
            if action == "deleted_by_receiver":
                set_status(conn, act, target, node, "deleted_by_receiver")
                continue
            if action == "origin_deleted":
                set_attr(page, "dm_status", "origin_deleted", attr_blocks(page))
                set_status(conn, act, target, node, "origin_deleted")
                continue
            if action == "conflict":
                parent = _roam_uid("conflict", page, str(node.version))
                if parent not in graph.blocks:
                    roam_target.create_block(
                        page,
                        f"dm_conflict:: the original changed (v{node.version}, {today}) after this copy was "
                        "edited here; incoming version below",
                        parent,
                        "last",
                    )
                    for i, line in enumerate([ln for ln in node.body.splitlines() if ln.strip()][:50]):
                        roam_target.create_block(
                            parent, line.strip()[:2000], _roam_uid("conflict-line", parent, str(i))
                        )
                    record_conflict(conn, act, target, node, page, parent)
                set_attr(page, "dm_status", "conflict", attr_blocks(page))
                put_record(conn, act, target, node, page, record["shared_hash"], "conflict")
                continue

            if not exists:
                page = _roam_uid("page", node.rid)
                roam_target.create_page(title, page)
                graph.pages[page] = title
            elif graph.pages[page] != title:
                roam_target.update_page(page, title)  # title changed at the origin
            uid_by_title[title] = page
            put_record(conn, act, target, node, page, write_mirror(page, node), "active")

        # Relations: add missing ones as outline structure; existing structure is never removed.
        graph = roam_target.read()
        existing_rels = {
            (r, graph.pages.get(s), graph.pages.get(d))
            for r, s, d, _ in roam.infer_relations(graph, roam.classify_pages(graph))
        }
        uid_by_title = {t: u for u, t in graph.pages.items()}
        for rel in relations:
            if rel.deleted or rel.label not in ("supports", "opposes", "informs"):
                continue
            src, dst = titles.get(rel.source), titles.get(rel.destination)
            if src not in uid_by_title or dst not in uid_by_title:
                continue
            if (rel.label, src, dst) in existing_rels or _roam_uid("rel", rel.key) in graph.blocks:
                act.count("relations_existing")
                continue
            if rel.label == "informs":
                roam_target.create_block(uid_by_title[dst], f"[[{src}]]", _roam_uid("rel", rel.key), "last")
            else:
                marker = _roam_uid("rel-marker", rel.key)
                roam_target.create_block(
                    uid_by_title[dst], f"[[{ROAM_RELATION_MARKERS[rel.label]}]]", marker, "last"
                )
                roam_target.create_block(marker, f"[[{src}]]", _roam_uid("rel", rel.key), "last")
            act.count("relations_added")
    return act.summary
