-- Fixes for the Supabase Security and Performance Advisors (cloud run, 2026-11-04).
--
-- 1. has_permission() is SECURITY DEFINER. In the public schema the API exposes it as
--    /rest/v1/rpc/has_permission to anon and authenticated users. Move it to a schema the API does
--    not serve. RLS policies reference the function by OID, so they keep working unchanged.
--    anon and authenticated still need EXECUTE, because policies run as the requesting role.
-- 2. Index every foreign key that had no covering index.

create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to anon, authenticated, service_role;

alter function public.has_permission(bigint, varchar) set schema private;
revoke execute on function private.has_permission(bigint, varchar) from public;
grant execute on function private.has_permission(bigint, varchar) to anon, authenticated, service_role;

create index if not exists "Concept_author_id_fkey_idx" on public."Concept" (author_id);
create index if not exists "Content_author_id_fkey_idx" on public."Content" (author_id);
create index if not exists "Content_creator_id_fkey_idx" on public."Content" (creator_id);
create index if not exists "Content_document_id_fkey_idx" on public."Content" (document_id);
create index if not exists "Content_part_of_id_fkey_idx" on public."Content" (part_of_id);
create index if not exists "Document_author_id_fkey_idx" on public."Document" (author_id);
create index if not exists "PlatformAccount_dg_account_fkey_idx" on public."PlatformAccount" (dg_account);
create index if not exists entity_revision_activity_id_fkey_idx on public.entity_revision (activity_id);
create index if not exists prov_activity_agent_id_fkey_idx on public.prov_activity (agent_id);
create index if not exists rbac_role_assignment_granted_by_fkey_idx on public.rbac_role_assignment (granted_by);
create index if not exists rbac_role_assignment_role_name_fkey_idx on public.rbac_role_assignment (role_name);
create index if not exists rbac_role_assignment_space_id_fkey_idx on public.rbac_role_assignment (space_id);
create index if not exists rbac_role_permission_permission_name_fkey_idx on public.rbac_role_permission (permission_name);
create index if not exists share_record_activity_id_fkey_idx on public.share_record (activity_id);
create index if not exists share_record_origin_concept_id_fkey_idx on public.share_record (origin_concept_id);
create index if not exists sync_conflict_activity_id_fkey_idx on public.sync_conflict (activity_id);
create index if not exists sync_conflict_origin_concept_id_fkey_idx on public.sync_conflict (origin_concept_id);
