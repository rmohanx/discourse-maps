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
def history(
    node: Annotated[str, typer.Argument(help='A node title (or part of it), or a "dm:<space>#<id>" rid')],
) -> None:
    """Every recorded version of a node, newest first, with the run that replaced it (docs/sync.md)."""
    from discourse_maps.importer import parse_rid

    with connect() as conn:
        if parsed := parse_rid(node):
            where, params = "s.url = %s and k.source_local_id = %s", parsed
        else:
            where, params = "k.name ilike %s and not k.is_schema and not k.is_relation", (f"%{node}%",)
        matches = conn.execute(
            f"""select k.id, k.name, s.url, k.literal_content, (k.reference_content->>'content')::bigint as content_id,
                       public.content_version((k.reference_content->>'content')::bigint) as version
                from "Concept" k join "Space" s on s.id = k.space_id where {where} order by k.id limit 5""",
            params,
        ).fetchall()
        if not matches:
            raise typer.BadParameter("No matching node.")
        for m in matches:
            deleted = " (deleted at origin)" if "deleted_at" in m["literal_content"] else ""
            typer.echo(f"\n{m['name']}{deleted}\n  space: {m['url']}   current version: v{m['version']}")
            for r in conn.execute(
                """
                select r.table_name, r.operation, r.replaced_at, r.data, a.activity_type, a.id as activity
                from entity_revision r left join prov_activity a on a.id = r.activity_id
                where r.entity_id in (%s, %s) order by r.replaced_at desc
                """,
                (m["id"], m["content_id"]),
            ):
                old = r["data"].get("text") or r["data"].get("name") or ""
                old = old.replace("\n", " ")[:90]
                typer.echo(
                    f"  {r['replaced_at']:%Y-%m-%d %H:%M}  {r['table_name']:<7} {r['operation']:<6} "
                    f"by {r['activity_type'] or '?'} #{r['activity'] or '-'}  was: {old}"
                )
            for s in conn.execute(
                "select target, status, origin_version, shared_at from share_record where origin_concept_id = %s",
                (m["id"],),
            ):
                typer.echo(
                    f"  shared to {s['target']}: v{s['origin_version']} {s['status']} ({s['shared_at']:%Y-%m-%d})"
                )


@app.command()
def conflicts() -> None:
    """Open sync conflicts: mirrors a receiver edited after which the original changed."""
    with connect() as conn:
        rows = conn.execute(
            """
            select c.target, k.name, c.mirror_ref, c.incoming_ref, c.detected_at
            from sync_conflict c join "Concept" k on k.id = c.origin_concept_id
            where c.resolved_at is null order by c.detected_at
            """
        ).fetchall()
        if not rows:
            typer.echo("  no open conflicts")
        for r in rows:
            typer.echo(
                f"  {r['detected_at']:%Y-%m-%d %H:%M}  {r['target']}\n    {r['name']}\n"
                f"    kept: {r['mirror_ref']}   incoming: {r['incoming_ref']}"
            )


@app.command()
def spaces() -> None:
    """List spaces (one per Roam graph, Obsidian vault, or import source) and their node counts."""
    with connect() as conn:
        for row in conn.execute(
            """
            select s.url, s.name, s.platform,
                   count(k.id) filter (where not k.is_schema and not k.is_relation) as nodes,
                   count(k.id) filter (where k.is_relation and not k.is_schema) as relations
            from "Space" s left join "Concept" k on k.space_id = s.id
            group by s.id order by s.id
            """
        ):
            typer.echo(
                f"  {row['platform']:<9} {row['nodes']:>4} nodes {row['relations']:>4} rels  {row['url']}"
            )


def _space_name(conn, url: str) -> str:
    row = conn.execute('select name from "Space" where url = %s', (url,)).fetchone()
    if not row:
        raise typer.BadParameter(f"No space {url!r}; see `dm spaces`.")
    return row["name"]


@app.command("share-to-obsidian")
def share_to_obsidian(
    from_space: Annotated[str, typer.Option("--from", help="Space URL to share (see `dm spaces`)")],
    vault: Annotated[Path, typer.Option(help="Receiving vault (with the Discourse Graph plugin)")],
    status: Annotated[str, typer.Option(help="all | accepted | candidate")] = "all",
) -> None:
    """Return trip: write another space's nodes and relations into a vault as shared copies."""
    from discourse_maps.share import share_to_obsidian as share

    with connect() as conn:
        _show(share(conn, from_space, _space_name(conn, from_space), vault.expanduser().resolve(), status))


@app.command("share-to-roam")
def share_to_roam(
    from_space: Annotated[str, typer.Option("--from", help="Space URL to share (see `dm spaces`)")],
    status: Annotated[str, typer.Option(help="all | accepted | candidate")] = "all",
) -> None:
    """Return trip: create another space's nodes and relations as pages in the Roam graph."""
    from discourse_maps import roam
    from discourse_maps.share import share_to_roam as share

    settings = get_settings()
    if not (settings.roam_graph and settings.roam_api_token):
        raise typer.BadParameter("Set ROAM_GRAPH and ROAM_API_TOKEN in .env.")
    client = roam.RoamClient(settings.roam_graph, settings.roam_api_token)
    with connect() as conn:
        _show(share(conn, from_space, _space_name(conn, from_space), client, status))


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
        typer.echo(f"{hit.score:6.3f}  {hit.platform:<8} {hit.year or '    '}  {hit.title[:100]}")


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
