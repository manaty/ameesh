-- SPDX-License-Identifier: AGPL-3.0-only
-- 0034_liaisons_de_session — liaison explicite d'une session externe à un
-- agent (lot L41, décision 0030 point 6 : « l'identité ne vient jamais du
-- dossier »).
--
-- Une session interactive (Claude, Codex, DeepSeek) lancée par un humain n'a
-- ni bail ni AGENT_MAIL_NAME. Son hook reçoit l'identifiant de session du
-- harnais (JSON sur stdin) : `ameesh mail bind <agent> --session <id>
-- --harness <h> [--pid N]` lie cet identifiant à un nom d'agent, sur cet hôte.
-- Le hook ne remet le courrier que si une liaison ACTIVE existe ; si `pid` est
-- renseigné, il doit être un ancêtre du processus du hook (le harnais qui a
-- lancé le hook), ce qui écarte un identifiant de session recopié ailleurs.
--
-- Une liaison n'est jamais effacée : `unbind` pose `revoked_at` (historique
-- lisible). Au plus UNE liaison active par (hôte, harnais, session).

create table if not exists session_bindings (
    id          bigserial primary key,
    session_id  text not null,
    harness     text not null,
    host        text not null,
    agent       text not null,
    pid         integer null,
    created_by  text not null,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz null,
    constraint session_bindings_session_id_ck
        check (length(session_id) between 1 and 200),
    constraint session_bindings_pid_ck check (pid is null or pid > 1)
);

create unique index if not exists session_bindings_active_uq
    on session_bindings (host, harness, session_id)
    where revoked_at is null;

create index if not exists session_bindings_agent_idx
    on session_bindings (agent) where revoked_at is null;

create index if not exists session_bindings_host_pid_idx
    on session_bindings (host, pid) where revoked_at is null and pid is not null;
