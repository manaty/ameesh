-- SPDX-License-Identifier: AGPL-3.0-only
-- 0037_historique_des_sessions — les sessions d'une persona, présentes et
-- passées (lot L52, décision 0032 §2 : une persona est définie aussi par ses
-- sessions ; étude v2 A7 et G1).
--
-- agent_registry.session_id ne garde que la session COURANTE ; une rotation
-- (0018), un relais de compte (L30) ou une session neuve l'écrasent. Cette
-- table en garde la trace : quand chaque session a commencé, sous quel harnais,
-- sur quel hôte et sous quel compte, pour quel lot, et quand et pourquoi elle a
-- pris fin. Aucun contenu : le transcript reste chez le harnais et dans sa
-- sauvegarde (L53).

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
