-- Sync history and state (see docs/sync.md).
--   entity_revision  every replaced or deleted version of Content / Concept rows, written by triggers,
--                    linked to the PROV-O activity that replaced it (session setting dm.activity_id)
--   share_record     sync state per (target tool, original node): where its mirror is, what we wrote
--   sync_conflict    receiver edits detected on re-share, with where the incoming version was put

create table public.entity_revision (
    id bigint generated always as identity primary key,
    entity_id bigint not null,               -- Content.id or Concept.id (shared entity_id_seq)
    table_name varchar not null check (table_name in ('Content', 'Concept')),
    operation varchar not null check (operation in ('update', 'delete')),
    data jsonb not null,                     -- the full row as it was before the change
    replaced_at timestamptz not null default now(),
    activity_id bigint references public.prov_activity (id) on delete set null
);

create index entity_revision_entity_idx on public.entity_revision (entity_id, replaced_at desc);

create or replace function public.record_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    insert into public.entity_revision (entity_id, table_name, operation, data, activity_id)
    values (
        old.id,
        tg_table_name,
        lower(tg_op),
        to_jsonb(old),
        nullif(current_setting('dm.activity_id', true), '')::bigint
    );
    return null;
end;
$$;

-- Only meaningful changes create a revision (not last_modified bumps).
create trigger content_revision
    after update of text, metadata on public."Content"
    for each row when (old.text is distinct from new.text)
    execute function public.record_revision();

create trigger content_revision_delete
    after delete on public."Content"
    for each row execute function public.record_revision();

create trigger concept_revision
    after update of name, literal_content, reference_content, epistemic_status, schema_id on public."Concept"
    for each row when (
        old.name is distinct from new.name
        or old.literal_content is distinct from new.literal_content
        or old.reference_content is distinct from new.reference_content
        or old.epistemic_status is distinct from new.epistemic_status
        or old.schema_id is distinct from new.schema_id
    )
    execute function public.record_revision();

create trigger concept_revision_delete
    after delete on public."Concept"
    for each row execute function public.record_revision();

create table public.share_record (
    target varchar not null,                 -- e.g. 'obsidian://vault/x' or 'roam://graph/y'
    origin_concept_id bigint not null references public."Concept" (id) on delete cascade,
    mirror_ref varchar not null,             -- vault-relative path, or Roam page uid
    shared_hash varchar not null,            -- hash of the mirror body we wrote (edit detection)
    origin_hash varchar not null,            -- hash of the origin content we shared (change detection)
    origin_version int not null,
    status varchar not null default 'active'
        check (status in ('active', 'conflict', 'deleted_by_receiver', 'origin_deleted')),
    shared_at timestamptz not null default now(),
    activity_id bigint references public.prov_activity (id) on delete set null,
    primary key (target, origin_concept_id)
);

create table public.sync_conflict (
    id bigint generated always as identity primary key,
    target varchar not null,
    origin_concept_id bigint not null references public."Concept" (id) on delete cascade,
    mirror_ref varchar not null,
    incoming_ref varchar,                    -- where the incoming version was written
    detected_at timestamptz not null default now(),
    resolved_at timestamptz,
    activity_id bigint references public.prov_activity (id) on delete set null
);

create index sync_conflict_open_idx on public.sync_conflict (target) where resolved_at is null;

-- Version number shown in mirrors: 1 + number of recorded text revisions of the node's Content.
create or replace function public.content_version(p_content_id bigint)
returns int
language sql
stable
set search_path = ''
as $$
    select 1 + count(*)::int from public.entity_revision
    where entity_id = p_content_id and table_name = 'Content' and operation = 'update';
$$;

-- RLS: readable by whoever may read the original node's space.
alter table public.entity_revision enable row level security;
alter table public.share_record enable row level security;
alter table public.sync_conflict enable row level security;

create policy revision_read on public.entity_revision for select to authenticated
    using (public.has_permission(((data->>'space_id')::bigint), 'content:read'));

create policy share_record_read on public.share_record for select to authenticated
    using (exists (select 1 from public."Concept" k
                   where k.id = origin_concept_id and public.has_permission(k.space_id, 'concept:read')));

create policy sync_conflict_read on public.sync_conflict for select to authenticated
    using (exists (select 1 from public."Concept" k
                   where k.id = origin_concept_id and public.has_permission(k.space_id, 'concept:read')));
