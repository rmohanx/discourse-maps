from pathlib import Path

import psycopg
import pytest

from discourse_maps import roam
from discourse_maps.db import connect

FIXTURE = Path(__file__).parent / "fixtures" / "roam_export.json"


def test_extract_page_refs_handles_nesting_and_tags():
    assert roam.extract_page_refs("see [[[[EVD]] - x]] and #tag") == ["EVD", "[[EVD]] - x", "tag"]


def test_classify_and_infer_relations():
    graph = roam.read_graph_export(FIXTURE, "test-graph")
    nodes = roam.classify_pages(graph)
    assert sorted(k for k, _ in nodes.values()) == [
        "claim",
        "claim",
        "evidence",
        "evidence",
        "question",
        "source",
        "source",
    ]
    rels = {(r, s, d) for r, s, d, _block in roam.infer_relations(graph, nodes)}
    assert rels == {
        ("informs", "e-compton", "q-size"),
        ("supports", "e-compton", "c-size"),
        ("opposes", "e-compton", "c-point"),
        ("supports", "e-dirac", "c-point"),
        ("derived_from", "e-compton", "s-compton"),
        ("derived_from", "e-dirac", "s-dirac"),
    }


def test_page_attributes():
    graph = roam.read_graph_export(FIXTURE, "test-graph")
    assert roam.page_attributes(graph, "e-dirac") == {
        "status": "candidate",
        "proposed_by": "claude-opus-5.5 (LLM)",
    }


@pytest.fixture
def conn():
    try:
        conn = connect()
    except psycopg.OperationalError:
        pytest.skip("local database not running (supabase start)")
    yield conn
    conn.rollback()
    conn.execute("delete from \"Space\" where url = 'roam://graph/test-graph-dm'")
    conn.commit()
    conn.close()


@pytest.mark.db
def test_import_graph_is_idempotent(conn):
    graph = roam.read_graph_export(FIXTURE, "test-graph-dm")
    first = roam.import_graph(conn, graph)
    assert first["evidence_candidate"] == 2 and first["claim_accepted"] == 1
    assert (
        first["relation_supports"] == 2 and first["relation_opposes"] == 1 and first["relation_informs"] == 1
    )
    assert "generated" not in roam.import_graph(conn, graph)
