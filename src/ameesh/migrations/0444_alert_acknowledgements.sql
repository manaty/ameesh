-- SPDX-License-Identifier: AGPL-3.0-only
-- L87c (#1463): a human recipient acknowledges one alert at its current
-- severity. Rows are retained as the audit trail, including after resolution.
-- No SECURITY DEFINER function or new grant; native operator connections only.
create table if not exists alert_acknowledgements (
    id bigserial primary key,
    alert_key text not null check (alert_key ~ '^[0-9a-f]{64}$'),
    severity text not null,
    -- Only that observer may declare resolution of a host-local sensor.
    observation_host text,
    actor text not null check (actor ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    principal text not null check (principal ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    acknowledged_at timestamptz not null default clock_timestamp(),
    -- Refresh on an idempotent retry without replacing its original audit.
    confirmed_at timestamptz not null default clock_timestamp(),
    invalidated_at timestamptz,
    invalidation_reason text check (invalidation_reason in ('resolved', 'severity_changed')),
    check ((invalidated_at is null) = (invalidation_reason is null))
);
create unique index if not exists alert_acknowledgements_live
    on alert_acknowledgements (alert_key, principal) where invalidated_at is null;
