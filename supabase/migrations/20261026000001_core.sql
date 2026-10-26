-- Core discourse-graph tables.
--
-- Table and column names follow DiscourseGraphs/discourse-graph (packages/database,
-- Apache-2.0) so data and ideas stay portable. This is an independent, minimal subset,
-- not a copy: no Vercel API, no sync semaphore, and RLS is handled in a later migration.
--
-- Differences from upstream, on purpose:
--   * Platform also has 'Bibtex' so non-Roam/Obsidian imports (e.g. Zitter) have a home.
--   * Natural-key UNIQUE constraints so every importer is idempotent (re-run = no dupes).
--   * Embedding model is a local Qwen3-Embedding-0.6B (1024 dims), following upstream's
--     one-table-per-model pattern.

create extension if not exists vector with schema extensions;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists unaccent with schema extensions;
create extension if not exists pgcrypto with schema extensions;

-- One id space for every entity (upstream pattern). Lets side tables such as
-- provenance or external identifiers point at "any entity" with a single bigint.
create sequence if not exists public.entity_id_seq;

create type public."Platform" as enum ('Roam', 'Obsidian', 'Bibtex');
create type public."AgentType" as enum ('person', 'organization', 'automated_agent', 'anonymous');
create type public."ContentVariant" as enum ('direct', 'direct_and_children', 'direct_and_description', 'full');
create type public."Scale" as enum (
    'document', 'post', 'chunk_unit', 'section', 'block',
    'field', 'paragraph', 'quote', 'sentence', 'phrase'
);
create type public."EpistemicStatus" as enum (
    'certainly_not', 'strong_evidence_against', 'could_be_false', 'unknown', 'uncertain',
    'contentious', 'could_be_true', 'strong_evidence_for', 'certain'
);
create type public."EmbeddingName" as enum ('qwen3_embedding_0_6b_1024');

create table public."Space" (
    id bigint primary key default nextval('public.entity_id_seq'),
    url varchar not null unique,
    name varchar not null,
    platform public."Platform" not null
);

create table public."PlatformAccount" (
    id bigint primary key default nextval('public.entity_id_seq'),
    name varchar not null,
    platform public."Platform" not null,
    account_local_id varchar not null,
    write_permission boolean not null default true,
    active boolean not null default true,
    agent_type public."AgentType" not null default 'person',
    metadata jsonb not null default '{}',
    dg_account uuid references auth.users (id) on delete set null on update cascade,
    unique (platform, account_local_id)
);

create table public."Document" (
    id bigint primary key default nextval('public.entity_id_seq'),
    space_id bigint references public."Space" (id) on delete cascade,
    source_local_id varchar,
    url varchar,
    created timestamp not null default now(),
    metadata jsonb not null default '{}',
    last_modified timestamp not null default now(),
    author_id bigint not null references public."PlatformAccount" (id),
    content_type varchar not null default 'text/plain',
    unique (space_id, source_local_id)
);

create table public."Content" (
    id bigint primary key default nextval('public.entity_id_seq'),
    document_id bigint not null references public."Document" (id) on delete cascade,
    source_local_id varchar,
    variant public."ContentVariant" not null default 'direct',
    author_id bigint references public."PlatformAccount" (id),
    creator_id bigint references public."PlatformAccount" (id),
    created timestamp not null default now(),
    text text not null,
    metadata jsonb not null default '{}',
    scale public."Scale" not null,
    space_id bigint references public."Space" (id) on delete cascade,
    last_modified timestamp not null default now(),
    part_of_id bigint references public."Content" (id) on delete set null,
    content_type varchar not null default 'text/plain',
    original boolean default true,
    -- Keyword search (ours; upstream uses pgroonga). 'simple' config keeps names and
    -- non-English titles (German papers in Zitter) intact.
    fts tsvector generated always as (to_tsvector('simple', text)) stored,
    unique (space_id, source_local_id, variant)
);

create index content_fts_idx on public."Content" using gin (fts);
create index content_text_trgm_idx on public."Content" using gin (text extensions.gin_trgm_ops);

-- Concept unifies node instances, relation instances, and their schemas (upstream design).
create table public."Concept" (
    id bigint primary key default nextval('public.entity_id_seq'),
    epistemic_status public."EpistemicStatus" not null default 'unknown',
    name varchar not null,
    description text,
    author_id bigint references public."PlatformAccount" (id),
    created timestamp not null default now(),
    last_modified timestamp not null default now(),
    space_id bigint not null references public."Space" (id) on delete cascade,
    arity smallint not null default 0,
    is_relation boolean not null default false,
    schema_id bigint references public."Concept" (id),
    literal_content jsonb not null default '{}',
    reference_content jsonb not null default '{}',
    is_schema boolean not null default false,
    source_local_id varchar,
    unique (space_id, source_local_id)
);

create index concept_schema_idx on public."Concept" (schema_id);
create index concept_refs_idx on public."Concept" using gin (reference_content jsonb_path_ops);

create table public."ContentEmbedding_qwen3_embedding_0_6b_1024" (
    target_id bigint primary key references public."Content" (id) on update cascade on delete cascade,
    model public."EmbeddingName" not null default 'qwen3_embedding_0_6b_1024',
    vector extensions.vector(1024) not null,
    obsolete boolean default false
);

create index content_embedding_qwen3_hnsw_idx
    on public."ContentEmbedding_qwen3_embedding_0_6b_1024"
    using hnsw (vector extensions.vector_cosine_ops);
