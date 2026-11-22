-- SPDX-License-Identifier: AGPL-3.0-only
do $migration$
declare
    target_schema text := current_schema();
    module_schema text;
begin
    select schema_name into module_schema from module_schemas where module = 'dev';
    if module_schema is not null then
        target_schema := module_schema;
    end if;
    execute format('alter table %I.ci_queue_state drop column if exists watch_failures', target_schema);
    execute format('alter table %I.ci_queue_state drop column if exists watch_failure_since', target_schema);
end
$migration$;
