-- SPDX-License-Identifier: AGPL-3.0-only
-- #919: pg_temp must be explicit and last for SECURITY DEFINER relation
-- lookups. ALTER preserves the function body, owner and restricted EXECUTE ACL.
do $$
begin
    execute format(
        'ALTER FUNCTION %I.ameesh_module_agent(text) SET search_path = %I, pg_temp',
        current_schema(), current_schema());
end $$;
