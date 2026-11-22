-- SPDX-License-Identifier: AGPL-3.0-only
-- #8071: preserve consecutive repository failures across watcher restarts.
-- The dev cutover can already have moved this table out of the core schema.
set local lock_timeout = '5s';
do $migration$
declare
    target_schema text := current_schema();
    module_schema text;
begin
    select schema_name into module_schema from module_schemas where module = 'dev';
    if module_schema is not null then
        target_schema := module_schema;
    end if;
    execute format('alter table %I.ci_queue_state add column if not exists watch_failures integer not null default 0', target_schema);
    execute format('alter table %I.ci_queue_state add column if not exists watch_failure_since timestamptz', target_schema);
end
$migration$;
