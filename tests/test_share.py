"""Sync scenarios from docs/sync.md, in both directions (Roam -> Obsidian and Obsidian -> Roam)."""

import copy
import json
import shutil
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest

from discourse_maps import roam
from discourse_maps.db import connect
from discourse_maps.obsidian_import import import_vault, split_frontmatter
from discourse_maps.share import SharedNode, bare_content, roam_title, share_to_obsidian, share_to_roam

FIXTURES = Path(__file__).parent / "fixtures"
ROAM_SPACE = "roam://graph/share-test-dm"
SHARED = "Shared/Maya's Roam"


def test_bare_content_strips_tool_prefixes():
    assert bare_content("[[CLM]] - Spin is motion") == "Spin is motion"
    assert bare_content("[[EVD]] - Huang found X - @Huang 1952") == "Huang found X"
    assert bare_content("SRC - Huang 1952") == "Huang 1952"
    assert bare_content("@Huang 1952") == "Huang 1952"


def test_roam_title_encodes_derived_from_in_evidence_title():
    evd = SharedNode("e", "dm:x#e", "Evidence", "Huang found X", "", None, None)
    assert roam_title(evd, {"s": "@Huang 1952"}, {"e": "s"}) == "[[EVD]] - Huang found X - @Huang 1952"


# ---------------------------------------------------------------- fixtures


class FakeRoam:
    """In-memory stand-in for a live Roam graph (same operations as share.RoamApiTarget)."""

    def __init__(self, name: str):
        self.name, self.graph = name, roam.RoamGraph(name)

    def read(self):
        return copy.deepcopy(self.graph)

    def _page_of(self, parent: str) -> str:
        return parent if parent in self.graph.pages else self.graph.blocks[parent]["page"]

    def create_page(self, title, uid):
        assert uid not in self.graph.pages
        self.graph.pages[uid] = title

    def update_page(self, uid, title):
        self.graph.pages[uid] = title

    def create_block(self, parent, text, uid, order="last"):
        assert uid not in self.graph.blocks, f"duplicate block uid {uid}"
        siblings = [b for b in self.graph.blocks.values() if b["parent"] == parent]
        self.graph.blocks[uid] = {
            "string": text,
            "order": len(siblings),
            "parent": parent,
            "page": self._page_of(parent),
        }
        by_title = {t: u for u, t in self.graph.pages.items()}
        for title in roam.extract_page_refs(text):  # like Roam, linking to a page creates it
            if title not in by_title:
                by_title[title] = f"auto-{len(self.graph.pages)}"
                self.graph.pages[by_title[title]] = title
        self.graph.refs[uid] = {by_title[t] for t in roam.extract_page_refs(text)}

    def update_block(self, uid, text):
        self.graph.blocks[uid]["string"] = text

    def delete_block(self, uid):
        for child in [u for u, b in self.graph.blocks.items() if b["parent"] == uid]:
            self.delete_block(child)
        self.graph.blocks.pop(uid)
        self.graph.refs.pop(uid, None)

    def delete_page(self, uid):
        for child in self.graph.children(uid):
            self.delete_block(child)
        self.graph.pages.pop(uid)


@pytest.fixture
def vault(tmp_path):
    target = tmp_path / "share-test-vault-dm"
    shutil.copytree(FIXTURES / "vault", target)
    settings = target / ".obsidian/plugins/discourse-graphs/data.json"
    data = json.loads(settings.read_text())  # give this vault the full default relation set
    data["relationTypes"] += [{"id": "rel_opp", "label": "opposes"}, {"id": "rel_inf", "label": "informs"}]
    settings.write_text(json.dumps(data))
    return target


@pytest.fixture
def conn(vault):
    try:
        conn = connect()
    except psycopg.OperationalError:
        pytest.skip("local database not running (supabase start)")
    yield conn
    conn.rollback()
    for url in (ROAM_SPACE, f"obsidian://vault/{quote(vault.name)}"):
        row = conn.execute('select id from "Space" where url = %s', (url,)).fetchone()
        if row:
            conn.execute('delete from "Space" where id = %s', (row["id"],))
            conn.execute("delete from entity_revision where (data->>'space_id')::bigint = %s", (row["id"],))
    conn.execute("delete from share_record where target like 'roam://graph/maya-fake-dm'")
    conn.commit()
    conn.close()


def maya_graph() -> roam.RoamGraph:
    return roam.read_graph_export(FIXTURES / "roam_export.json", "share-test-dm")


def share(conn, vault):
    return share_to_obsidian(conn, ROAM_SPACE, "Maya's Roam", vault)


def mirror(vault: Path, prefix: str) -> Path:
    return next(p for p in (vault / SHARED).glob("*.md") if p.name.startswith(prefix))


# ---------------------------------------------------------------- Roam -> Obsidian


@pytest.mark.db
def test_first_share_and_round_trip_without_duplicates(conn, vault):
    roam.import_graph(conn, maya_graph())
    first = share(conn, vault)  # S1
    assert first["create"] == 7 and first["relations_written"] == 6
    front, body = split_frontmatter(mirror(vault, "EVD - Compton").read_text())
    assert front["dm_status"] == "latest" and front["dm_version"] == 1
    assert "full history is in the database" in body.lower()

    assert share(conn, vault)["unchanged"] == 7  # S2

    summary = import_vault(conn, vault)  # I6: mirrors map to Maya's originals
    assert summary["shared_copies_mapped"] == 7 and summary["skipped_relation_shared_copy"] == 6


@pytest.mark.db
def test_origin_edit_refreshes_mirror_and_keeps_history(conn, vault):
    graph = maya_graph()
    roam.import_graph(conn, graph)
    share(conn, vault)

    graph.blocks["e3"]["string"] = (
        "> Compton concluded the electron's diameter matches hard gamma-ray wavelengths."
    )
    roam.import_graph(conn, graph)  # I3
    result = share(conn, vault)  # S3
    assert result["refresh"] == 1 and result["unchanged"] == 6
    front, body = split_frontmatter(mirror(vault, "EVD - Compton").read_text())
    assert front["dm_version"] == 2 and "diameter matches" in body

    history = conn.execute(
        """select r.data->>'text' as old_text, a.activity_type from entity_revision r
           join prov_activity a on a.id = r.activity_id
           join "Space" s on s.id = (r.data->>'space_id')::bigint
           where r.table_name = 'Content' and r.data->>'source_local_id' = 'e-compton' and s.url = %s""",
        (ROAM_SPACE,),
    ).fetchall()
    assert len(history) == 1 and "comparable in magnitude" in history[0]["old_text"]
    assert history[0]["activity_type"] == "import:roam"  # PROV-O: which run replaced it


@pytest.mark.db
def test_receiver_edit_is_never_overwritten(conn, vault):
    graph = maya_graph()
    roam.import_graph(conn, graph)
    share(conn, vault)
    note = mirror(vault, "EVD - Compton")
    note.write_text(note.read_text() + "\nSam's own aside.\n")  # receiver edits the mirror

    assert share(conn, vault)["edited_no_update"] == 1  # nothing incoming: leave it alone

    graph.blocks["e3"]["string"] = "> A revised quote from Maya."
    roam.import_graph(conn, graph)
    assert share(conn, vault)["conflict"] == 1  # S4
    front, body = split_frontmatter(note.read_text())
    assert "Sam's own aside." in body and front["dm_status"] == "conflict"
    incoming = vault / front["dm_conflict_file"]
    assert "A revised quote from Maya." in incoming.read_text()
    assert "nodeTypeId" not in split_frontmatter(incoming.read_text())[0]  # not a node; importer skips it
    target = f"obsidian://vault/{quote(vault.name)}"
    open_conflicts = conn.execute(
        "select count(*) as n from sync_conflict where target = %s and resolved_at is null", (target,)
    ).fetchone()["n"]
    assert open_conflicts == 1
    assert share(conn, vault)["edited_no_update"] == 1  # no repeated conflict for the same update


@pytest.mark.db
def test_receiver_deletion_is_respected(conn, vault):
    roam.import_graph(conn, maya_graph())
    share(conn, vault)
    mirror(vault, "QUE - What is the size").unlink()  # S5
    assert share(conn, vault)["deleted_by_receiver"] == 1
    assert not list((vault / SHARED).glob("QUE - *.md"))  # not re-created


@pytest.mark.db
def test_origin_deletion_flags_mirror_and_keeps_it(conn, vault):
    graph = maya_graph()
    roam.import_graph(conn, graph)
    share(conn, vault)
    for uid in [u for u, b in graph.blocks.items() if b["page"] == "q-size"]:
        graph.blocks.pop(uid)
    graph.pages.pop("q-size")  # Maya deletes her Question page
    assert roam.import_graph(conn, graph)["marked_deleted"] >= 1  # I4: marked, not removed
    assert share(conn, vault)["origin_deleted"] == 1  # S6
    front, _ = split_frontmatter(mirror(vault, "QUE - What is the size").read_text())
    assert front["dm_status"] == "origin_deleted"


# ---------------------------------------------------------------- Obsidian -> Roam


@pytest.mark.db
def test_share_to_roam_scenarios(conn, vault):
    import_vault(conn, vault)  # Sam's vault is the origin this time
    space = f"obsidian://vault/{quote(vault.name)}"
    fake = FakeRoam("maya-fake-dm")

    first = share_to_roam(conn, space, "Sam's vault", fake)  # S1
    assert first["create"] == 3
    page = next(u for u, t in fake.graph.pages.items() if t.startswith("[[EVD]] - Huang 1952"))
    attrs = roam.page_attributes(fake.graph, page)
    assert attrs["dm_status"] == "latest" and attrs["dm_version"] == "1" and "imported_from_rid" in attrs
    assert first["relations_added"] == 1  # Evidence supports Claim -> [[SupportedBy]] structure

    assert share_to_roam(conn, space, "Sam's vault", fake)["unchanged"] == 3  # S2

    evd_note = vault / "EVD - Huang 1952 read Zitterbewegung as circular motion.md"
    evd_note.write_text(
        evd_note.read_text().replace("a circular motion", "a circular motion (Sam's revision)")
    )
    import_vault(conn, vault)
    assert share_to_roam(conn, space, "Sam's vault", fake)["refresh"] == 1  # S3
    lines = [fake.graph.blocks[u]["string"] for u in fake.graph.children(page)]
    assert any("Sam's revision" in line for line in lines)
    assert roam.page_attributes(fake.graph, page)["dm_version"] == "2"

    revised = next(u for u in fake.graph.children(page) if "Sam's revision" in fake.graph.blocks[u]["string"])
    fake.update_block(revised, "Maya annotated this line.")  # receiver edits the mirror
    evd_note.write_text(evd_note.read_text().replace("Sam's revision", "Sam's second revision"))
    import_vault(conn, vault)
    assert share_to_roam(conn, space, "Sam's vault", fake)["conflict"] == 1  # S4
    strings = [b["string"] for b in fake.graph.blocks.values() if b["page"] == page]
    assert "Maya annotated this line." in strings  # her edit kept
    assert any(s.startswith("dm_conflict::") for s in strings)

    claim_page = next(u for u, t in fake.graph.pages.items() if t.startswith("[[CLM]]"))
    fake.delete_page(claim_page)  # S5
    assert share_to_roam(conn, space, "Sam's vault", fake)["deleted_by_receiver"] == 1
    assert not any(t.startswith("[[CLM]]") for t in fake.graph.pages.values())
