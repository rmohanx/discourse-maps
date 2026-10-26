-- Standards layers on top of the core tables. These are additions of ours; none of them
-- change upstream table shapes, so the core stays compatible with discourse-graph.
--
--   external_identifier  persistent identifiers (DOI per ISO 26324, OpenAlex, arXiv, URL per RFC 3986)
--   prov_activity / prov_generation   W3C PROV-O: which activity, run by which agent, produced which entity
--   rbac_*               role-based access control (NIST RBAC model: users, roles, permissions, sessions-free)

-- Persistent identifiers --------------------------------------------------------------

create table public.external_identifier (
    entity_id bigint not null,          -- any entity id from entity_id_seq (usually a Document)
    scheme varchar not null check (scheme in ('doi', 'openalex', 'arxiv', 'url', 'bibtex_key')),
    value varchar not null,
    primary key (scheme, value),
    unique (entity_id, scheme)
);

create index external_identifier_entity_idx on public.external_identifier (entity_id);

-- Provenance (PROV-O) -----------------------------------------------------------------

create table public.prov_activity (
    id bigint primary key default nextval('public.entity_id_seq'),
    activity_type varchar not null,      -- e.g. 'import:bibtex', 'enrich:openalex', 'embed:qwen3'
    agent_id bigint references public."PlatformAccount" (id),   -- prov:wasAssociatedWith
    started_at timestamptz not null default now(),               -- prov:startedAtTime
    ended_at timestamptz,                                        -- prov:endedAtTime
    status varchar not null default 'running' check (status in ('running', 'complete', 'failed')),
    parameters jsonb not null default '{}',                      -- inputs, versions, model names
    summary jsonb not null default '{}'                          -- counts, errors
);

-- prov:wasGeneratedBy (first creation) and later revisions by other activities.
create table public.prov_generation (
    entity_id bigint not null,
    activity_id bigint not null references public.prov_activity (id) on delete cascade,
    role varchar not null default 'generated' check (role in ('generated', 'revised', 'derived')),
    at timestamptz not null default now(),
    primary key (entity_id, activity_id)
);

create index prov_generation_activity_idx on public.prov_generation (activity_id);

-- RBAC ----------------------------------------------------------------------------------
-- Upstream has three space-level levels (partial / reader / editor). We model named roles
-- with explicit permissions so policies can ask "may X do Y here?" instead of comparing levels.

create table public.rbac_role (
    name varchar primary key,
    description text not null,
    upstream_equivalent varchar           -- closest SpaceAccessPermissions value upstream
);

create table public.rbac_permission (
    name varchar primary key,             -- '<resource>:<action>', e.g. 'content:read'
    description text not null
);

create table public.rbac_role_permission (
    role_name varchar not null references public.rbac_role (name) on delete cascade,
    permission_name varchar not null references public.rbac_permission (name) on delete cascade,
    primary key (role_name, permission_name)
);

create table public.rbac_role_assignment (
    account_id bigint not null references public."PlatformAccount" (id) on delete cascade,
    space_id bigint not null references public."Space" (id) on delete cascade,
    role_name varchar not null references public.rbac_role (name),
    granted_at timestamptz not null default now(),
    granted_by bigint references public."PlatformAccount" (id),
    primary key (account_id, space_id, role_name)
);

insert into public.rbac_role (name, description, upstream_equivalent) values
    ('viewer',      'Read published content and concepts',                    'partial'),
    ('reader',      'Read everything in the space, including drafts',         'reader'),
    ('contributor', 'Read, and propose concepts and annotations',              'editor'),
    ('curator',     'Contributor, plus accept/reject proposals and edit any', 'editor'),
    ('admin',       'Curator, plus manage roles in the space',                'editor');

insert into public.rbac_permission (name, description) values
    ('content:read',        'Read Content and Document rows'),
    ('content:write',       'Create or update Content and Document rows'),
    ('concept:read',        'Read Concept rows'),
    ('concept:propose',     'Create Concepts with a proposed status'),
    ('concept:curate',      'Change epistemic status or edit others'' Concepts'),
    ('role:manage',         'Grant and revoke roles in the space');

insert into public.rbac_role_permission (role_name, permission_name)
select r.name, p.name
from public.rbac_role r
join public.rbac_permission p on case r.name
    when 'viewer'      then p.name in ('content:read', 'concept:read')
    when 'reader'      then p.name in ('content:read', 'concept:read')
    when 'contributor' then p.name in ('content:read', 'concept:read', 'concept:propose')
    when 'curator'     then p.name <> 'role:manage'
    when 'admin'       then true
end;

-- The single question RLS policies will ask. auth.uid() maps to an account via
-- PlatformAccount.dg_account. Enabling RLS on the core tables is a later migration.
create or replace function public.has_permission(p_space_id bigint, p_permission varchar)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1
        from public.rbac_role_assignment ra
        join public."PlatformAccount" pa on pa.id = ra.account_id
        join public.rbac_role_permission rp on rp.role_name = ra.role_name
        where pa.dg_account = auth.uid()
          and pa.active
          and ra.space_id = p_space_id
          and rp.permission_name = p_permission
    );
$$;
