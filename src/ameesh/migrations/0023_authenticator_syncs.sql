-- SPDX-License-Identifier: AGPL-3.0-only
-- 0023_authenticator_syncs — journal des synchronisations canon → registre des
-- authentificateurs (spec §4.1, §8.2 ; lot L9b, revue B1/B2).
--
-- Chaque synchronisation appliquée (`canon_sync.sync_authenticators`) ajoute
-- une ligne : le commit du membre racine du canon, les commits de chaque
-- source lue (`commits` = {membre: commit}), la branche canonique de
-- CONFIANCE contre laquelle le commit racine a été vérifié (suivi distant,
-- ex. `origin/main`) et l'origine de cette confiance :
--
-- * `config`    : configuration de l'hôte (AMEESH_CANON_REF / `canon_ref`) ;
-- * `applied`   : manifeste du dernier commit déjà appliqué (ligne précédente) ;
-- * `bootstrap` : premier amorçage explicite (`canon sync --bootstrap-ref`).
--
-- La dernière ligne est le « dernier commit appliqué » : relue sous le verrou
-- consultatif transactionnel du registre (pris AVANT tout contrôle), elle
-- fonde la règle de monotonie — un commit qui n'en descend pas n'est jamais
-- écrit (pas de retour en arrière). Une ligne n'est jamais modifiée.
create table if not exists authenticator_syncs (
    id          bigserial primary key,
    root_member text not null,
    root_commit text not null
                check (root_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    commits     jsonb not null check (jsonb_typeof(commits) = 'object'),
    branch      text not null check (branch <> ''),
    trust       text not null check (trust in ('config', 'applied', 'bootstrap')),
    host        text not null default '',
    applied_by  text not null default current_user,
    applied_at  timestamptz not null default now(),
    summary     jsonb
);
