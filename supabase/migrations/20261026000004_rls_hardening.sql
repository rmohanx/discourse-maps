-- RLS hardening found by scripts/api_user_demo.py and the Supabase advisor.
--  * Proposals must be authored by the caller's own account (no proposing in someone else's name).
--  * Wrap auth.uid() in a scalar subquery so it is evaluated once per statement (advisor: auth_rls_initplan).

drop policy concept_propose on public."Concept";
create policy concept_propose on public."Concept" for insert to authenticated
    with check (
        public.has_permission(space_id, 'concept:propose')
        and epistemic_status in ('unknown', 'uncertain')
        and author_id in (select id from public."PlatformAccount" where dg_account = (select auth.uid()))
    );

drop policy account_self on public."PlatformAccount";
create policy account_self on public."PlatformAccount" for select to authenticated
    using (dg_account = (select auth.uid()));

drop policy assignment_self on public.rbac_role_assignment;
create policy assignment_self on public.rbac_role_assignment for select to authenticated
    using (account_id in (select id from public."PlatformAccount" where dg_account = (select auth.uid())));
