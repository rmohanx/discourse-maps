"""Import a Roam Research graph (Discourse Graph extension conventions) into the database.

Roam is a graph of pages and nested blocks; every block has a uid and can reference pages.
Reading it: the Roam backend API (Datalog queries with a graph token) or a JSON export.

Discourse nodes are pages whose titles follow the extension's default formats:
    [[QUE]] - {content}   [[CLM]] - {content}   [[EVD]] - {content} - {Source}   @{content}
Relations are not stored; the extension infers them from outline structure, and so do we:
    informs   a block on a Question page references an Evidence page
    supports  a block referencing [[SupportedBy]] has a child that references an Evidence page,
              under (a block referencing, or the page of) a Claim; or the mirror form with [[Supports]]
    opposes   the same with [[OpposedBy]] / [[Opposes]]
    derived from  an Evidence title's "- {Source}" part names a Source page
Mapping: node page -> Document + Content (title and outline) + Concept; relation -> relation Concept.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Jsonb

from discourse_maps import db
from discourse_maps.importer import (
    RELATION_SCHEMAS,
    ensure_schemas,
    find_same_work,
    parse_rid,
    relation_exists_elsewhere,
    resolve_rid,
    upsert_returning,
)

API = "https://api.roamresearch.com/api/graph"
NODE_PATTERNS = {
    "question": re.compile(r"^\[\[QUE\]\] - (?P<content>.+)$"),
    "claim": re.compile(r"^\[\[CLM\]\] - (?P<content>.+)$"),
    "evidence": re.compile(r"^\[\[EVD\]\] - (?P<content>.+?)(?: - (?P<source>.+))?$"),
    "source": re.compile(r"^@(?P<content>.+)$"),
}
# (marker page title, relation key, which end the parent block points at)
STRUCTURAL_RELATIONS = [
    ("SupportedBy", "supports", "destination"),
    ("OpposedBy", "opposes", "destination"),
    ("Supports", "supports", "source"),
    ("Opposes", "opposes", "source"),
]
_TAG = re.compile(r"(?:^|\s)#([\w/-]+)")


def extract_page_refs(text: str) -> list[str]:
    """Page titles referenced by [[...]] (nesting allowed, e.g. [[[[EVD]] - x]]) or #tag."""
    titles, stack, i = [], [], 0
    while i < len(text) - 1:
        pair = text[i : i + 2]
        if pair == "[[":
            stack.append(i + 2)
            i += 2
        elif pair == "]]" and stack:
            titles.append(text[stack.pop() : i])
            i += 2
        else:
            i += 1
    return titles + _TAG.findall(text)


@dataclass
class RoamGraph:
    name: str
    pages: dict[str, str] = field(default_factory=dict)  # uid -> title
    blocks: dict[str, dict] = field(default_factory=dict)  # uid -> {string, order, parent, page}
    refs: dict[str, set[str]] = field(default_factory=dict)  # block uid -> referenced page uids

    def children(self, parent: str) -> list[str]:
        return sorted(
            (u for u, b in self.blocks.items() if b["parent"] == parent),
            key=lambda u: self.blocks[u]["order"],
        )

    def outline(self, parent: str, depth: int = 0) -> list[str]:
        lines = []
        for uid in self.children(parent):
            lines.append("  " * depth + "- " + self.blocks[uid]["string"])
            lines += self.outline(uid, depth + 1)
        return lines


# ---------------------------------------------------------------- reading


class RoamClient:
    """Minimal Roam backend API client. The API redirects to a peer host; we re-send the auth header
    ourselves because HTTP clients drop it on cross-host redirects."""

    def __init__(self, graph: str, token: str):
        self.graph, self.http = graph, httpx.Client(timeout=60)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.base = f"{API}/{graph}"

    def q(self, query: str) -> list:
        url = f"{self.base}/q"
        for _ in range(8):
            resp = self.http.post(url, headers=self.headers, json={"query": query})
            if resp.status_code == 429:  # rate limited: wait and retry
                time.sleep(int(resp.headers.get("retry-after", 10)))
                continue
            if resp.status_code in (307, 308):
                url = resp.headers["location"]
                self.base = url.rsplit("/", 1)[0]  # remember the peer for later calls
                continue
            resp.raise_for_status()
            return resp.json()["result"]
        raise RuntimeError("Roam API kept redirecting")


def read_graph_api(client: RoamClient) -> RoamGraph:
    graph = RoamGraph(client.graph)
    for uid, title in client.q("[:find ?uid ?title :where [?p :node/title ?title] [?p :block/uid ?uid]]"):
        graph.pages[uid] = title
    for uid, string, order, parent, page in client.q(
        """[:find ?uid ?s ?o ?par-uid ?pg-uid
            :where [?b :block/string ?s] [?b :block/uid ?uid] [(get-else $ ?b :block/order 0) ?o]
                   [?par :block/children ?b] [?par :block/uid ?par-uid]
                   [?b :block/page ?pg] [?pg :block/uid ?pg-uid]]"""
    ):
        graph.blocks[uid] = {"string": string, "order": order, "parent": parent, "page": page}
    for uid, ref in client.q(
        "[:find ?uid ?ruid :where [?b :block/refs ?r] [?r :node/title] [?b :block/uid ?uid] [?r :block/uid ?ruid]]"
    ):
        graph.refs.setdefault(uid, set()).add(ref)
    return graph


def read_graph_export(path: Path, name: str | None = None) -> RoamGraph:
    """Roam's JSON export: [{title, uid, children: [{string, uid, children}]}]. Refs come from [[links]]."""
    pages = json.loads(path.read_text(encoding="utf-8"))
    graph = RoamGraph(name or path.stem)
    for page in pages:
        graph.pages[page["uid"]] = page["title"]
    by_title = {t: u for u, t in graph.pages.items()}

    def walk(children: list, parent: str, page_uid: str) -> None:
        for order, child in enumerate(children or []):
            graph.blocks[child["uid"]] = {
                "string": child.get("string", ""),
                "order": child.get("order", order),
                "parent": parent,
                "page": page_uid,
            }
            titles = extract_page_refs(child.get("string", ""))
            graph.refs[child["uid"]] = {by_title[t] for t in titles if t in by_title}
            walk(child.get("children"), child["uid"], page_uid)

    for page in pages:
        walk(page.get("children"), page["uid"], page["uid"])
    return graph


# ---------------------------------------------------------------- interpreting


def classify_pages(graph: RoamGraph) -> dict[str, tuple[str, re.Match]]:
    nodes = {}
    for uid, title in graph.pages.items():
        for kind, pattern in NODE_PATTERNS.items():
            if m := pattern.match(title):
                nodes[uid] = (kind, m)
                break
    return nodes


_ATTRIBUTE = re.compile(r"^(?P<key>[\w -]+)::\s*(?P<value>.*)$")


def page_attributes(graph: RoamGraph, page_uid: str) -> dict[str, str]:
    """Roam attributes on the page's top-level blocks, e.g. `status:: candidate`."""
    attrs = {}
    for uid in graph.children(page_uid):
        if m := _ATTRIBUTE.match(graph.blocks[uid]["string"]):
            attrs[m["key"].strip().lower().replace(" ", "_")] = m["value"].strip()
    return attrs


def infer_relations(
    graph: RoamGraph, nodes: dict[str, tuple[str, re.Match]]
) -> set[tuple[str, str, str, str]]:
    """Return {(relation_key, source_page_uid, destination_page_uid, evidence_block_uid)}."""
    kind = {uid: k for uid, (k, _) in nodes.items()}
    title_uid = {t: u for u, t in graph.pages.items()}
    found = set()

    def refs_of_kind(block: str, wanted: str) -> list[str]:
        return [r for r in graph.refs.get(block, ()) if kind.get(r) == wanted]

    for uid, block in graph.blocks.items():
        # informs: Evidence referenced from a block on a Question page
        if kind.get(block["page"]) == "question":
            for evd in refs_of_kind(uid, "evidence"):
                found.add(("informs", evd, block["page"], uid))

        # supports / opposes: marker block whose children reference the other end
        ref_titles = {graph.pages.get(r) for r in graph.refs.get(uid, ())}
        for marker, rel, parent_end in STRUCTURAL_RELATIONS:
            if marker not in ref_titles:
                continue
            parent = block["parent"]
            parent_wants = "claim" if parent_end == "destination" else "evidence"
            child_wants = "evidence" if parent_end == "destination" else "claim"
            anchors = [parent] if kind.get(parent) == parent_wants else refs_of_kind(parent, parent_wants)
            for child in graph.children(uid):
                for other in refs_of_kind(child, child_wants):
                    for anchor in anchors:
                        src, dst = (other, anchor) if parent_end == "destination" else (anchor, other)
                        found.add((rel, src, dst, child))

    # derived from: "[[EVD]] - content - @Source title" (or a [[link]] to it)
    for uid, (k, m) in nodes.items():
        if k == "evidence" and m.group("source"):
            name = m.group("source").strip()
            name = re.sub(r"^\[\[(.*)\]\]$", r"\1", name)
            src_uid = title_uid.get(name) or title_uid.get(f"@{name}")
            if src_uid and kind.get(src_uid) == "source":
                found.add(("derived_from", uid, src_uid, uid))
    return found


# ---------------------------------------------------------------- writing


def import_graph(conn: psycopg.Connection, graph: RoamGraph) -> dict[str, int]:
    nodes = classify_pages(graph)
    relations = infer_relations(graph, nodes)
    space_url = f"roam://graph/{graph.name}"
    space_id = db.ensure_space(conn, space_url, f"Roam graph: {graph.name}", platform="Roam")
    pipeline = db.ensure_agent(conn)
    owner = conn.execute(
        """
        insert into "PlatformAccount" (name, platform, account_local_id, agent_type)
        values (%s, 'Roam', %s, 'person')
        on conflict (platform, account_local_id) do update set name = excluded.name returning id
        """,
        (f"{graph.name} graph owner", f"graph-owner:{graph.name}"),
    ).fetchone()["id"]

    with db.activity(conn, "import:roam", pipeline, {"graph": graph.name, "space": space_url}) as act:
        schemas = ensure_schemas(conn, space_id, pipeline)
        act.count("pages_total", len(graph.pages))
        concept_ids = {}
        for uid, (kind, _match) in nodes.items():
            title = graph.pages[uid]
            attrs = page_attributes(graph, uid)
            if parse_rid(attrs.get("imported_from_rid")):
                # A shared copy of someone else's node: map it to the original, don't duplicate it.
                original = resolve_rid(conn, attrs["imported_from_rid"])
                if original:
                    concept_ids[uid] = original
                act.count("shared_copies_mapped" if original else "shared_copies_orphaned")
                continue
            status = attrs.get("status", "accepted")  # hand-written pages count as accepted
            proposer = attrs.get("proposed_by")
            author = owner
            if proposer:
                author = conn.execute(
                    """
                    insert into "PlatformAccount" (name, platform, account_local_id, agent_type)
                    values (%s, 'Roam', %s, 'automated_agent')
                    on conflict (platform, account_local_id) do update set name = excluded.name returning id
                    """,
                    (proposer, f"agent:{proposer}"),
                ).fetchone()["id"]
            text = "\n".join([title, "", *graph.outline(uid)]).strip()
            url = f"https://roamresearch.com/#/app/{graph.name}/page/{uid}"
            doc_id, inserted = upsert_returning(
                conn,
                """
                insert into "Document" (space_id, source_local_id, url, author_id, metadata)
                values (%s, %s, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set url = excluded.url, metadata = excluded.metadata, last_modified = now()
                returning id, (xmax = 0) as inserted
                """,
                (space_id, uid, url, owner, Jsonb({"title": title, "roam_uid": uid})),
            )
            act.record(doc_id, inserted)
            content_id, inserted = upsert_returning(
                conn,
                """
                insert into "Content" (document_id, space_id, source_local_id, text, scale, author_id, creator_id)
                values (%s, %s, %s, %s, 'document', %s, %s)
                on conflict (space_id, source_local_id, variant) do update
                    set text = excluded.text,
                        last_modified = case when "Content".text = excluded.text
                                             then "Content".last_modified else now() end
                returning id, (xmax = 0) as inserted
                """,
                (doc_id, space_id, uid, text, owner, owner),
            )
            act.record(content_id, inserted)
            conn.execute(
                """
                update "ContentEmbedding_qwen3_embedding_0_6b_1024" e set obsolete = true
                from "Content" c where c.id = e.target_id and c.id = %s and c.last_modified = now()
                """,
                (content_id,),
            )
            reference = {"document": doc_id, "content": content_id}
            if kind == "source" and (
                same := find_same_work(conn, space_id, attrs.get("doi"), attrs.get("zitter_key"))
            ):
                reference["same_work_as_document"] = same
            concept_id, inserted = upsert_returning(
                conn,
                """
                insert into "Concept" (name, space_id, author_id, schema_id, literal_content,
                                       reference_content, source_local_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set name = excluded.name, author_id = excluded.author_id,
                        literal_content = excluded.literal_content,
                        reference_content = excluded.reference_content, last_modified = now()
                returning id, (xmax = 0) as inserted
                """,
                (
                    title,
                    space_id,
                    author,
                    schemas[kind],
                    Jsonb(
                        {
                            "review_status": status,
                            "proposed_by": proposer,
                            "node_type": kind.capitalize(),
                            "roam_uid": uid,
                        }
                    ),
                    Jsonb(reference),
                    f"node:{uid}",
                ),
            )
            act.record(concept_id, inserted)
            act.count(f"{kind}_{status}")
            concept_ids[uid] = concept_id

        for rel, src_uid, dst_uid, evidence_block in sorted(relations):
            src, dst = concept_ids.get(src_uid), concept_ids.get(dst_uid)
            if src is None or dst is None:
                act.count("skipped_relation_missing_node")
                continue
            if relation_exists_elsewhere(conn, space_id, RELATION_SCHEMAS[rel], src, dst):
                act.count("skipped_relation_shared_copy")  # already held by the origin space
                continue
            rel_id, inserted = upsert_returning(
                conn,
                """
                insert into "Concept" (name, space_id, author_id, schema_id, is_relation, arity,
                                       literal_content, reference_content, source_local_id)
                values (%s, %s, %s, %s, true, 2, %s, %s, %s)
                on conflict (space_id, source_local_id) do update
                    set reference_content = excluded.reference_content
                returning id, (xmax = 0) as inserted
                """,
                (
                    RELATION_SCHEMAS[rel],
                    space_id,
                    owner,
                    schemas[rel],
                    Jsonb({"inferred_from_block": evidence_block}),
                    Jsonb({"source": src, "destination": dst}),
                    f"rel:{rel}:{src_uid}->{dst_uid}",
                ),
            )
            act.record(rel_id, inserted)
            act.count(f"relation_{rel}")
    return act.summary
