-- SPDX-License-Identifier: AGPL-3.0-only
-- #4867 / #2202 S1: dormant until the runner supervisor is installed.
create table sandbox_turns (
    turn_id          text primary key,             -- turn_resources.turn_id
    spend_key        text not null unique,         -- one per turn; admission idempotency key
    agent            text not null,
    owner            text not null,
    lease_epoch      bigint not null,              -- agent_registry.lease_epoch is bigint
    sandbox_session  text,                         -- reviewer session, never the ameesh harness session
    request_ids      bigint[] not null default '{}',
    work_item_id     bigint,
    scope            jsonb not null,               -- repo, pr, head, base, generation, attempt
    scope_digest     text not null,                -- sha256(JCS(scope)), exact per attempt
    retry_family     text not null,                -- sha256(JCS(scope without attempt))
    scope_generation integer not null,
    attempt          integer not null,             -- server-assigned: family count + 1
    max_attempts     integer not null,
    unique (retry_family, attempt),
    profile_ref      text not null,
    profile_digest   text not null,
    placement_ref    text not null,
    placement_digest text not null,
    canon_ref        text not null,                -- canon revision of the admission
    tranche_id       text,
    limits           jsonb not null,               -- immutable snapshot
    image_digest     text not null,
    provider         text not null,                -- kubernetes
    namespace        text not null,
    job_name         text not null unique,         -- deterministic
    job_uid          text unique,                  -- bound once
    job_phase        text,
    state            text not null default 'intent'
        check (state in ('intent','created','bound','running','cancelling',
                         'succeeded','failed','cancelled','expired','orphaned','lost')),
    result_id        text unique,
    result_sha256    text,
    refusal          text,
    cancel_requested_at timestamptz,
    cancel_observed_at  timestamptz,
    deadline_ts      timestamptz not null,
    started_at       timestamptz not null default now(),
    completed_at     timestamptz,
    source           jsonb not null                -- release/commit/contract
);
create index sandbox_turns_open on sandbox_turns (agent, state, deadline_ts);
create index sandbox_turns_job on sandbox_turns (namespace, job_name);

create table sandbox_results (
    turn_id       text primary key references sandbox_turns(turn_id),
    epoch         bigint not null,
    result_id     text not null unique,            -- client idempotency id
    result_sha256 text not null,
    payload       jsonb not null,                  -- bounded, sanitized
    receipt       jsonb not null,                  -- ameesh-sandbox-receipt/1
    accepted_at   timestamptz not null default now()
);

-- A surviving Job consumes the agent's slot, even after its lease is lost.
create unique index sandbox_turns_live_agent on sandbox_turns (agent)
    where state not in ('succeeded','failed','cancelled','expired','lost');

-- Admission facts and a bound UID cannot be silently rewritten by recovery.
create function ameesh_sandbox_snapshot_guard() returns trigger
language plpgsql as $$
begin
    if (to_jsonb(new) - array['sandbox_session','job_uid','job_phase','state',
            'result_id','result_sha256','refusal','cancel_requested_at',
            'cancel_observed_at','completed_at']) is distinct from
       (to_jsonb(old) - array['sandbox_session','job_uid','job_phase','state',
            'result_id','result_sha256','refusal','cancel_requested_at',
            'cancel_observed_at','completed_at']) then
        raise exception 'immutable sandbox admission';
    end if;
    if old.job_uid is not null and new.job_uid is distinct from old.job_uid then
        raise exception 'job-conflict';
    end if;
    if old.cancel_requested_at is not null and
       new.cancel_requested_at is distinct from old.cancel_requested_at then
        raise exception 'immutable cancellation fence';
    end if;
    return new;
end $$;
create trigger sandbox_turns_snapshot_guard before update on sandbox_turns
    for each row execute function ameesh_sandbox_snapshot_guard();
