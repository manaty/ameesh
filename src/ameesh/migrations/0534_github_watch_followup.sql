-- SPDX-License-Identifier: AGPL-3.0-only
-- 0534_github_watch_followup — #5506 : une entrée remise au veilleur reste
-- ouverte jusqu’à la preuve de son traitement.
--
-- Constat (ameesh#4065 → #5503) : l’entrée a attendu 6 h 10 avant d’être
-- convertie en lot ; rien ne la relançait. Chaque entrée livrée (issue ou
-- commentaire) est suivie ici ; la passe de veille la ferme sur une preuve
-- lue par le code (lot porteur de l’issue_ref, note de lot citant l’URL,
-- réponse du mesh marquée sur le ticket), relance le veilleur à 60 min et
-- escalade au pilote à 2 h. Additive : une table neuve, aucune donnée touchée.
-- events_after : borne du journal des lots au dépôt ; une note postérieure a
-- un id plus grand, lue par l’index primaire plutôt que par un parcours du
-- journal. checked_at : dernière vérification de la preuve ; la passe
-- (processus neuf à chaque minuterie) ne relit une entrée qu’après
-- FOLLOW_UP_EVERY.
CREATE TABLE github_watch_pending (
    project text NOT NULL,
    repo text NOT NULL,
    kind text NOT NULL,
    object_id bigint NOT NULL,
    issue_ref text NOT NULL,
    url text NOT NULL,
    agent text NOT NULL,
    delivered_at double precision NOT NULL,
    events_after bigint NOT NULL DEFAULT 0,
    reminded_at double precision,
    escalated_at double precision,
    resolved_at double precision,
    checked_at double precision,
    proof text,
    PRIMARY KEY (project, repo, kind, object_id)
);
CREATE INDEX github_watch_pending_open_idx
    ON github_watch_pending (delivered_at) WHERE resolved_at IS NULL;
-- #4299 : le veilleur est un nom d’agent, réécrit au renommage et normalisé à
-- l’écriture comme les autres colonnes d’identité.
create trigger ameesh_agent_spelling before insert or update of agent on github_watch_pending
    for each row execute function ameesh_agent_spelling('agent');
