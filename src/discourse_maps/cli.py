"""Command line: `uv run dm <command>`. Run `uv run dm --help` for the list."""

from pathlib import Path
from typing import Annotated

import typer

from discourse_maps import zitter
from discourse_maps.config import get_settings
from discourse_maps.db import connect

app = typer.Typer(no_args_is_help=True, help="Discourse Maps: plumbing for discourse graphs on Supabase.")


def _show(summary: dict) -> None:
    for key, value in summary.items():
        typer.echo(f"  {key}: {value}")


@app.command()
def fetch() -> None:
    """Download Zitter BibTeX files and the paper list into data/raw/zitter/."""
    _show(zitter.fetch())


@app.command("fetch-pdfs")
def fetch_pdfs(limit: Annotated[int | None, typer.Option(help="Stop after N papers")] = None) -> None:
    """Download Zitter PDFs into data/pdfs/ (git-ignored; slow on purpose)."""
    _show(zitter.fetch_pdfs(limit))


@app.command("import")
def import_() -> None:
    """Import the downloaded BibTeX files as Sources (idempotent)."""
    from discourse_maps.bibtex import parse_file
    from discourse_maps.importer import import_records

    files = sorted(zitter.RAW_DIR.glob("*.bib"))
    if not files:
        raise typer.BadParameter("No BibTeX files found; run `dm fetch` first.")
    records = [r for f in files for r in parse_file(f)]
    with connect() as conn:
        _show(import_records(conn, records))


@app.command("import-obsidian")
def import_obsidian(
    vault: Annotated[Path, typer.Argument(help="Vault folder with the Discourse Graph plugin")],
) -> None:
    """Import a researcher's Obsidian vault (nodes, review status, relations). Idempotent."""
    from discourse_maps.obsidian_import import import_vault

    with connect() as conn:
        _show(import_vault(conn, vault.expanduser().resolve()))


@app.command("import-roam")
def import_roam(
    export: Annotated[Path | None, typer.Option(help="Roam JSON export instead of the live API")] = None,
) -> None:
    """Import a Roam graph (Discourse Graph conventions) via the backend API or a JSON export."""
    from discourse_maps import roam

    if export:
        graph = roam.read_graph_export(export)
    else:
        settings = get_settings()
        if not (settings.roam_graph and settings.roam_api_token):
            raise typer.BadParameter("Set ROAM_GRAPH and ROAM_API_TOKEN in .env, or pass --export.")
        graph = roam.read_graph_api(roam.RoamClient(settings.roam_graph, settings.roam_api_token))
    with connect() as conn:
        _show(roam.import_graph(conn, graph))


@app.command()
def enrich(
    limit: Annotated[int | None, typer.Option(help="Only enrich N documents")] = None,
    force: Annotated[bool, typer.Option(help="Re-query documents already enriched")] = False,
) -> None:
    """Add abstracts, identifiers, and citations from OpenAlex / Crossref."""
    from discourse_maps.enrich import enrich_space

    with connect() as conn:
        _show(enrich_space(conn, limit=limit, force=force))


@app.command()
def embed() -> None:
    """Embed Content that is new or changed since the last run."""
    from discourse_maps.embeddings import embed_pending, get_provider

    provider = get_provider(get_settings().embedding_provider)
    with connect() as conn:
        _show(embed_pending(conn, provider))


@app.command()
def search(
    query: str,
    mode: Annotated[str, typer.Option(help="keyword | fuzzy | semantic")] = "semantic",
    k: Annotated[int, typer.Option(help="Number of results")] = 10,
    serendipity: Annotated[float, typer.Option(min=0.0, max=1.0, help="0 = closest, 1 = most varied")] = 0.0,
) -> None:
    """Search Sources."""
    from discourse_maps import search as s

    if serendipity and mode != "semantic":
        raise typer.BadParameter("--serendipity only applies to --mode semantic")
    with connect() as conn:
        if mode == "keyword":
            hits = s.keyword(conn, query, k)
        elif mode == "fuzzy":
            hits = s.fuzzy(conn, query, k)
        elif mode == "semantic":
            from discourse_maps.embeddings import get_provider

            hits = s.semantic(conn, get_provider(get_settings().embedding_provider), query, k, serendipity)
        else:
            raise typer.BadParameter("mode must be keyword, fuzzy, or semantic")
    for hit in hits:
        typer.echo(f"{hit.score:6.3f}  {hit.year or '????'}  {hit.key:<12} {hit.title}")


@app.command("export-obsidian")
def export_obsidian(
    vault: Annotated[Path, typer.Option(help="Vault folder")] = Path("data/obsidian-vault"),
) -> None:
    """Write the Zitter space to an Obsidian vault (one note per Source)."""
    from discourse_maps.importer import ZITTER_SPACE
    from discourse_maps.obsidian import export_space

    with connect() as conn:
        _show(export_space(conn, ZITTER_SPACE[0], vault))
    typer.echo(f"  vault: {vault.resolve()}")


@app.command()
def status() -> None:
    """Row counts and the latest pipeline runs."""
    with connect() as conn:
        counts = conn.execute(
            """
            select (select count(*) from "Document") as documents,
                   (select count(*) from "Content") as contents,
                   (select count(*) from "Concept" where not is_schema and not is_relation) as nodes,
                   (select count(*) from "Concept" where is_relation and not is_schema) as relations,
                   (select count(*) from "ContentEmbedding_qwen3_embedding_0_6b_1024" where not obsolete) as embeddings,
                   (select count(*) from external_identifier) as identifiers
            """
        ).fetchone()
        _show(counts)
        typer.echo("  recent activities:")
        for row in conn.execute(
            "select id, activity_type, status, started_at, summary from prov_activity order by id desc limit 5"
        ):
            typer.echo(f"    #{row['id']} {row['activity_type']:<26} {row['status']:<9} {row['summary']}")
