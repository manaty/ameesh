-- SPDX-License-Identifier: AGPL-3.0-only
-- 0066_persona_session_history — current and past persona sessions (L52).
-- Reserved on lot #139. Decision 0032 §2; v2 study A7 and G1.
-- The registry holds the current session; this table retains its harness,
-- host, account, lot, start/end times and end reason across rotations.
-- No transcript content: harness storage and L53 backups retain it.

create table if not exists persona_sessions (
    id          bigserial primary key,
    persona     text not null,
    session_id  text not null,
    harness     text null,
    host        text null,
    account     text null,
    work_item   text null,
    started_at  timestamptz not null default now(),
    ended_at    timestamptz null,
    end_reason  text null,
    constraint persona_sessions_unique unique (persona, session_id),
    constraint persona_sessions_session_id_ck check (length(session_id) between 1 and 200)
);

create index if not exists persona_sessions_persona_idx
    on persona_sessions (persona, started_at desc);
