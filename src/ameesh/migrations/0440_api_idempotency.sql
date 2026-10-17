-- SPDX-License-Identifier: AGPL-3.0-only
-- 0440_api_idempotency — the `Idempotency-Key` of the API's actions (L87, lot
-- #375; embeddable interface study §2.1).
--
-- Every `POST /api/v1/...` carries an `Idempotency-Key`. The key is reserved
-- here BEFORE the action runs, scoped to the human who sent it; the response
-- is stored once the action has answered. A retry with the same key and the
-- same request reads the stored response back and never acts twice; the same
-- key with another request is refused; a key whose action is still running
-- is refused as busy. A reservation whose action failed for a transient
-- reason (database unavailable, internal error) is released, so the client
-- can retry. Rows older than a day are purged on the next reservation.
-- Repeatable.

create table if not exists api_idempotency (
    human        text not null check (human ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    key          text not null check (key ~ '^[A-Za-z0-9_-]{8,128}$'),
    -- sha256 of the method, the path and the canonical body
    fingerprint  text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    status       integer check (status between 200 and 599),
    response     jsonb,
    created_at   timestamptz not null default now(),
    done_at      timestamptz,
    primary key (human, key),
    check ((status is null) = (response is null)),
    check ((status is null) = (done_at is null))
);
create index if not exists api_idempotency_created on api_idempotency (created_at);
