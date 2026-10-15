-- SPDX-License-Identifier: AGPL-3.0-only
-- 0445_provenance_receipts — la preuve exacte d'origine d'un commit et d'un
-- jalon (lot #1465).
--
-- Le train de paquets (#1467) qualifie chaque commit de la tête par l'acteur,
-- la session, le tour et le harnais qui l'ont produit. Une jointure temporelle
-- (« le tour le plus proche ») n'est PAS une preuve : elle peut désigner le tour
-- d'un autre agent, ou le tour qui a commenté plutôt que celui qui a poussé.
--
-- Cette table est la source unique du fournisseur `ameesh.review_provenance` :
-- une ligne écrite AU MOMENT du fait, par l'outil qui le produit —
-- `ameesh provenance record --commit <sha>` (paire de hooks `pre-commit` /
-- `post-commit` : l'attestation, puis le fait de création de l'objet né, donc
-- pour chaque commit) pour un commit, la déclaration du jalon pour un verdict
-- (même transaction que `work_item_milestones`). `sha` est le commit COMPLET
-- (40 ou 64 hex) : une abréviation citée par un jalon est résolue à la
-- déclaration, quand le dépôt est sous la main.
--
-- Absence de ligne = origine inconnue. Le fournisseur rend alors `unknown`,
-- jamais un voisin temporel ; le qualificateur refuse (pas de GO implicite).
-- La table est en AJOUT SEUL : une seconde écriture du même sujet ne réécrit
-- pas la preuve (le fournisseur relit la première et compare l'identité).
--
-- L'acteur d'une ligne de commit est celui de la session VIVANTE qui a exécuté
-- l'outil (registre), jamais un nom fourni par l'appelant. `work_item_id` est
-- facultatif : il rattache la preuve au lot, il ne la conditionne pas.

create table if not exists provenance_receipts (
    subject      text primary key,
    sha          text not null,
    actor        text not null,
    session      text not null,
    turn         text not null,
    harness      text not null,
    work_item_id bigint references work_items(id) on delete set null,
    at           timestamptz not null default now(),
    constraint provenance_receipts_subject_ck check (
        subject ~ '^commit:[0-9a-f]{40}([0-9a-f]{24})?$'
        or subject ~ '^milestone:[0-9]+$'),
    constraint provenance_receipts_sha_ck check (sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    constraint provenance_receipts_actor_ck check (length(actor) between 1 and 200),
    constraint provenance_receipts_session_ck check (length(session) between 1 and 200),
    constraint provenance_receipts_turn_ck check (length(turn) between 1 and 200),
    constraint provenance_receipts_harness_ck check (length(harness) between 1 and 200)
);

create index if not exists provenance_receipts_sha_idx
    on provenance_receipts (sha);

comment on table provenance_receipts is
  '#1465 : preuve exacte (acteur, session, tour, harnais) de l''origine d''un commit ou d''un jalon, écrite au moment du fait. Ajout seul ; l''absence vaut origine inconnue, jamais une jointure temporelle.';
