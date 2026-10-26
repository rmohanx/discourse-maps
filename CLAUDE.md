# discourse-maps

Python pipeline for discourse graphs on local Supabase. See README.md for commands and design.

## Conventions
- Python >= 3.14, managed with `uv`. Run things as `uv run dm ...`, `uv run pytest`, `uv run ruff check .`.
- Schema changes go in new files under `supabase/migrations/` (timestamped); apply with
  `supabase migration up --local` (`supabase db reset` rebuilds from scratch and wipes data).
- Core table names follow upstream DiscourseGraphs/discourse-graph `packages/database`; don't rename them.
  Our additions live in separate tables (standards layers), not new columns on core tables.
- Every importer must be idempotent (upserts on natural keys) and run inside `db.activity(...)` so
  provenance is recorded.
- Never commit secrets (`.env`) or anything under `data/` (downloads, PDFs, generated vault).
- DB tests are marked `db` and use their own Space, which they delete afterwards.
- Importers: `obsidian_import.py` (vault, plugin format) and `roam.py` (API/export). Both write Document +
  Content + Concept; review state lives in `Concept.literal_content.review_status` / `proposed_by`.
- RLS is on for every public table; API access is governed by `has_permission()`. The pipeline connects
  as `postgres` and bypasses RLS.

## Gotchas
- OpenAlex needs `OPENALEX_API_KEY` (free); anonymous calls share a small per-IP daily budget.
  On 429 with a long Retry-After, `dm enrich` stops cleanly and resumes on the next run.
- Docker Desktop VM is capped at 4 GB; Supabase runs with realtime, storage, edge runtime,
  analytics, and the mail server disabled (see `supabase/config.toml`).
