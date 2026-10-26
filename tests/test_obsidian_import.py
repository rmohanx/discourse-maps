import json
import shutil
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest

from discourse_maps.db import connect
from discourse_maps.obsidian_import import import_vault, read_vault

FIXTURE = Path(__file__).parent / "fixtures" / "vault"


def test_read_vault_nodes_status_and_relations():
    vault = read_vault(FIXTURE)
    by_title = {n.title: n for n in vault.notes}
    assert set(by_title) == {
        "CLM - Spin is internal motion",
        "EVD - Huang 1952 read Zitterbewegung as circular motion",
        "SRC - Huang 1952",
    }
    assert vault.skipped["not_a_node"] == 1
    claim = by_title["CLM - Spin is internal motion"]
    assert claim.key == "path:CLM - Spin is internal motion.md"  # no nodeInstanceId yet
    labels = sorted(r["type_label"] for r in vault.relations)
    assert labels == ["derived from", "supports"]  # relations.json + legacy frontmatter link


@pytest.fixture
def vault_copy(tmp_path):
    target = tmp_path / "test-vault-dm"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def conn(vault_copy):
    try:
        conn = connect()
    except psycopg.OperationalError:
        pytest.skip("local database not running (supabase start)")
    yield conn
    conn.rollback()
    conn.execute('delete from "Space" where url = %s', (f"obsidian://vault/{quote(vault_copy.name)}",))
    conn.commit()
    conn.close()


def _concepts(conn, vault: Path) -> list[dict]:
    return conn.execute(
        """
        select k.name, k.is_relation, k.literal_content->>'review_status' as status, pa.name as author
        from "Concept" k join "Space" s on s.id = k.space_id
        join "PlatformAccount" pa on pa.id = k.author_id
        where s.url = %s and not k.is_schema order by k.is_relation, k.name
        """,
        (f"obsidian://vault/{quote(vault.name)}",),
    ).fetchall()


@pytest.mark.db
def test_import_vault_is_idempotent_and_keeps_review_state(conn, vault_copy):
    first = import_vault(conn, vault_copy)
    assert first["notes"] == 3 and first["relation_supports"] == 1 and first["relation_derived_from"] == 1
    assert "generated" not in import_vault(conn, vault_copy)

    rows = {r["name"]: r for r in _concepts(conn, vault_copy)}
    evd = rows["EVD - Huang 1952 read Zitterbewegung as circular motion"]
    assert (evd["status"], evd["author"]) == ("candidate", "test-llm")
    assert rows["CLM - Spin is internal motion"]["status"] == "accepted"
    assert sum(r["is_relation"] for r in rows.values()) == 2


@pytest.mark.db
def test_plugin_assigning_an_id_later_does_not_duplicate(conn, vault_copy):
    import_vault(conn, vault_copy)
    note = vault_copy / "CLM - Spin is internal motion.md"
    note.write_text(
        note.read_text().replace("nodeTypeId: node_c", 'nodeTypeId: node_c\nnodeInstanceId: "clm-1"')
    )
    second = import_vault(conn, vault_copy)
    assert "generated" not in second
    assert len([r for r in _concepts(conn, vault_copy) if not r["is_relation"]]) == 3


@pytest.mark.db
def test_custom_node_and_relation_types_are_imported(conn, vault_copy):
    settings_path = vault_copy / ".obsidian/plugins/discourse-graphs/data.json"
    settings = json.loads(settings_path.read_text())
    settings["nodeTypes"].append({"id": "node_h", "name": "Hypothesis", "format": "HYP - {content}"})
    settings["relationTypes"].append({"id": "rel_tests", "label": "tests"})
    settings_path.write_text(json.dumps(settings))
    (vault_copy / "HYP - Electrons have a toroidal moment.md").write_text(
        '---\nnodeTypeId: node_h\nnodeInstanceId: "hyp-1"\nrel_tests: "[[SRC - Huang 1952]]"\n---\n'
    )

    summary = import_vault(conn, vault_copy)
    assert summary["hypothesis_accepted"] == 1 and summary["relation_tests"] == 1
    schemas = conn.execute(
        """select k.name from "Concept" k join "Space" s on s.id = k.space_id
           where s.url = %s and k.is_schema and k.literal_content->>'custom' = 'true' order by k.name""",
        (f"obsidian://vault/{quote(vault_copy.name)}",),
    ).fetchall()
    assert [r["name"] for r in schemas] == ["Hypothesis", "tests"]
