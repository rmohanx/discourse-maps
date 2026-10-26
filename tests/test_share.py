import json
import shutil
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest

from discourse_maps import roam
from discourse_maps.db import connect
from discourse_maps.obsidian_import import import_vault, split_frontmatter
from discourse_maps.share import SharedNode, bare_content, roam_title, share_to_obsidian

FIXTURES = Path(__file__).parent / "fixtures"
ROAM_SPACE = "roam://graph/share-test-dm"


def test_bare_content_strips_tool_prefixes():
    assert bare_content("[[CLM]] - Spin is motion") == "Spin is motion"
    assert bare_content("[[EVD]] - Huang found X - @Huang 1952") == "Huang found X"
    assert bare_content("SRC - Huang 1952") == "Huang 1952"
    assert bare_content("@Huang 1952") == "Huang 1952"


def test_roam_title_encodes_derived_from_in_evidence_title():
    evd = SharedNode("e", "dm:x#e", "Evidence", "Huang found X", "", None, None)
    assert roam_title(evd, {"s": "@Huang 1952"}, {"e": "s"}) == "[[EVD]] - Huang found X - @Huang 1952"


@pytest.fixture
def vault(tmp_path):
    target = tmp_path / "share-test-vault-dm"
    shutil.copytree(FIXTURES / "vault", target)
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
        conn.execute('delete from "Space" where url = %s', (url,))
    conn.commit()
    conn.close()


def _count(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()["n"]


@pytest.mark.db
def test_roam_to_obsidian_round_trip(conn, vault):
    roam.import_graph(conn, roam.read_graph_export(FIXTURES / "roam_export.json", "share-test-dm"))

    # Share Maya's Roam graph into Sam's vault.
    counts = share_to_obsidian(conn, ROAM_SPACE, "Maya's Roam", vault)
    # The fixture vault only defines supports + derived from, so opposes / informs can't be expressed there.
    assert counts["notes_written"] == 7 and counts["relations_written"] == 4
    assert counts["skipped_relation_type"] == 2
    shared = sorted((vault / "Shared" / "Maya's Roam").glob("*.md"))
    front, _ = split_frontmatter(shared[0].read_text())
    assert front["importedFromRid"].startswith(f"dm:{ROAM_SPACE}#")

    # Sam links a shared Evidence note to his own Claim.
    rels = json.loads((vault / "relations.json").read_text())
    evd = next(p for p in shared if p.name.startswith("EVD - Compton"))
    evd_id = split_frontmatter(evd.read_text())[0]["nodeInstanceId"]
    claim = vault / "CLM - Spin is internal motion.md"
    claim.write_text(
        claim.read_text().replace("nodeTypeId: node_c", 'nodeTypeId: node_c\nnodeInstanceId: "clm-1"')
    )
    rels["relations"]["sam-1"] = {
        "id": "sam-1",
        "type": "rel_sup",
        "source": evd_id,
        "destination": "clm-1",
        "created": 0,
    }
    (vault / "relations.json").write_text(json.dumps(rels))

    # Importing Sam's vault: shared copies map to Maya's originals; only Sam's own work is new.
    summary = import_vault(conn, vault)
    assert summary["shared_copies_mapped"] == 7
    assert summary["skipped_relation_shared_copy"] == 4
    vault_space = f"obsidian://vault/{quote(vault.name)}"
    assert (
        _count(
            conn,
            """select count(*) as n from "Concept" k join "Space" s on s.id = k.space_id
                           where s.url = %s and not k.is_schema and not k.is_relation""",
            vault_space,
        )
        == 3
    )
    original_evd = conn.execute(
        """select k.id from "Concept" k join "Space" s on s.id = k.space_id
           where s.url = %s and k.name like '[[EVD]] - Compton%%'""",
        (ROAM_SPACE,),
    ).fetchone()["id"]
    assert (
        _count(
            conn,
            """select count(*) as n from "Concept" k join "Space" s on s.id = k.space_id
                           where s.url = %s and k.is_relation and not k.is_schema
                             and (k.reference_content->>'source')::bigint = %s""",
            vault_space,
            original_evd,
        )
        == 1
    )

    # Sharing again refreshes the same files instead of adding new ones.
    share_to_obsidian(conn, ROAM_SPACE, "Maya's Roam", vault)
    assert len(list((vault / "Shared" / "Maya's Roam").glob("*.md"))) == 7
