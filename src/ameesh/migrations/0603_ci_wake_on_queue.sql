-- SPDX-License-Identifier: AGPL-3.0-only
-- 0603_ci_wake_on_queue — #8031 : réveiller une VM CI sur un job en file.
--
-- Constat : `vm_adapter.wake_due` ne réveille un hôte que pour du travail
-- d'agent (lots avec `wake_ts`, fenêtres de release, retour demandé). Une fois
-- les VM de CI arrêtées au repos (#8029), un job qui attend un runner les
-- laisserait éteintes indéfiniment : le relevé de la file (#845, 0426) est la
-- seule mesure qui voit cette attente, et il ne conservait ni les étiquettes
-- du job ni celles du runner qui pourraient le servir.
--
-- Modèle ajouté (migration ADDITIVE) :
--
-- * `ci_queue_wait` : les jobs qu'**aucun runner en ligne** ne peut prendre,
--   une ligne par dépôt et par jeu d'étiquettes demandées. C'est plus étroit
--   que la file de capacité (`ci_queue_state`, #845/#7015/#7935) : un runner
--   en ligne mais occupé prendra le job au créneau suivant, aucune VM n'est
--   appelée ; seul un job qu'aucun runner en ligne ne porte prouve qu'une VM
--   arrêtée manque à la file. `labels` est le jeu trié et replié en minuscules
--   (GitHub compare les étiquettes sans casse) ; `oldest_id`/`oldest_at`
--   datent le plus vieux job du groupe, `queued` en compte les jobs,
--   `seen_at` date le relevé. Le relevé d'un dépôt REMPLACE ses lignes dans
--   la même passe : une ligne absente n'est pas « zéro job » mais du bruit
--   d'un relevé incomplet (`ci_guard_scan.complete`), et le lecteur ne s'en
--   sert pas.
-- * `ci_runner_state.labels` : les étiquettes de chaque runner enregistré du
--   dépôt, en ligne ou hors ligne (GitHub les rend encore quand la VM est
--   arrêtée). Elles donnent la correspondance hôte → étiquettes servies, sans
--   nouvelle inscription : le nom du runner (`ameesh-ci-1-3`) rattache le
--   runner à l'hôte `ameesh-ci-1`. NULL = relevé d'avant 0603, donc
--   illisible : le doute interdit l'arrêt, jamais le réveil.
-- * `ci_queue_state.wait_seen_at` : l'instant où le relevé courant a publié
--   `ci_queue_wait`, estampillé dans la MÊME écriture que `seen_at`. Une ligne
--   `ci_queue_state` fraîche dont `wait_seen_at` est NULL ou diffère de
--   `seen_at` vient d'un veilleur N-1 (qui rafraîchit la file de capacité sans
--   publier les groupes) ou d'une publication interrompue : le lecteur doute
--   au lieu de lire une file de réveil vide.
-- * `ci_queue_wait_scan` : une ligne par dépôt écrite ATOMIQUEMENT avec les
--   groupes, au même `seen_at` qu'eux. Le lecteur exige que ce stamp précède
--   ou égale `wait_seen_at` : des groupes publiés dont la file n'a pas été
--   estampillée (arrêt entre les deux écritures) laissent `scan > wait_seen`,
--   donc le doute, même quand aucun groupe n'attend. C'est la preuve de
--   complétude qui manquerait à une simple absence de lignes.
--
-- Aucune écriture hors du veilleur `ci.Watcher`. Les deux colonnes et les deux
-- tables sont classées au contrat du rôle superviseur (état : dépôt, étiquettes,
-- horodatages, comptages ; aucun contenu), comme `ci_queue_state`. Le rollback
-- (`rollback/0603_restore.sql`) retire les quatre objets.
set local lock_timeout = '5s';

alter table ci_runner_state
    add column if not exists labels jsonb;

-- #8031 : preuve que le relevé de capacité et la file de réveil viennent de
-- la même passe complète (NULL = veilleur N-1 ou publication interrompue).
alter table ci_queue_state
    add column if not exists wait_seen_at timestamptz;

-- #8031 : stamp atomique de la publication des groupes, y compris vide.
create table if not exists ci_queue_wait_scan (
    repo    text primary key,
    seen_at timestamptz not null
);

create table if not exists ci_queue_wait (
    repo      text not null,
    -- jeu trié et replié des étiquettes demandées par les jobs du groupe
    labels    jsonb not null,
    -- plus vieux job du groupe, et nombre de jobs dans ce cas
    oldest_id bigint,
    oldest_at timestamptz,
    queued    integer not null default 0,
    -- date du relevé complet qui a publié cette ligne
    seen_at   timestamptz not null default now(),
    primary key (repo, labels)
);

create index if not exists ci_queue_wait_seen_idx
    on ci_queue_wait (repo, seen_at);
