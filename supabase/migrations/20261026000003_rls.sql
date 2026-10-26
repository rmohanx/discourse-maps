-- Row-level security on every public table, driven by has_permission() (RBAC layer).
--
-- Who sees what through the HTTP API (PostgREST):
--   anon           nothing (no policies)
--   authenticated  rows in spaces where their PlatformAccount holds a role with the permission
-- Direct connections as `postgres` (our Python pipeline) bypass RLS, as owners do.

alter table public."Space" enable row level security;
alter table public."PlatformAccount" enable row level security;
alter table public."Document" enable row level security;
alter table public."Content" enable row level security;
alter table public."Concept" enable row level security;
alter table public."ContentEmbedding_qwen3_embedding_0_6b_1024" enable row level security;
alter table public.external_identifier enable row level security;
alter table public.prov_activity enable row level security;
alter table public.prov_generation enable row level security;
alter table public.rbac_role enable row level security;
alter table public.rbac_permission enable row level security;
alter table public.rbac_role_permission enable row level security;
alter table public.rbac_role_assignment enable row level security;

-- Content ---------------------------------------------------------------------------

create policy space_read on public."Space" for select to authenticated
    using (public.has_permission(id, 'content:read'));

create policy document_read on public."Document" for select to authenticated
    using (public.has_permission(space_id, 'content:read'));

create policy content_read on public."Content" for select to authenticated
    using (public.has_permission(space_id, 'content:read'));

create policy embedding_read on public."ContentEmbedding_qwen3_embedding_0_6b_1024" for select to authenticated
    using (exists (
        select 1 from public."Content" c
        where c.id = target_id and public.has_permission(c.space_id, 'content:read')
    ));

create policy identifier_read on public.external_identifier for select to authenticated
    using (exists (
        select 1 from public."Document" d
        where d.id = entity_id and public.has_permission(d.space_id, 'content:read')
    ));

-- Concepts: read, propose (new rows start unknown/uncertain), curate (change anything) ---

create policy concept_read on public."Concept" for select to authenticated
    using (public.has_permission(space_id, 'concept:read'));

create policy concept_propose on public."Concept" for insert to authenticated
    with check (
        public.has_permission(space_id, 'concept:propose')
        and epistemic_status in ('unknown', 'uncertain')
    );

create policy concept_curate on public."Concept" for update to authenticated
    using (public.has_permission(space_id, 'concept:curate'))
    with check (public.has_permission(space_id, 'concept:curate'));

-- Accounts and roles: see yourself, your assignments, and the role catalog ---------------

create policy account_self on public."PlatformAccount" for select to authenticated
    using (dg_account = auth.uid());

create policy assignment_self on public.rbac_role_assignment for select to authenticated
    using (account_id in (select id from public."PlatformAccount" where dg_account = auth.uid()));

create policy role_catalog on public.rbac_role for select to authenticated using (true);
create policy permission_catalog on public.rbac_permission for select to authenticated using (true);
create policy role_permission_catalog on public.rbac_role_permission for select to authenticated using (true);

-- Provenance: readable by signed-in users (it describes pipeline runs, not private content) ---

create policy prov_activity_read on public.prov_activity for select to authenticated using (true);
create policy prov_generation_read on public.prov_generation for select to authenticated using (true);
