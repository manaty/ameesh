-- SPDX-License-Identifier: AGPL-3.0-only
-- 0566_provenance_revocations — retirer une preuve d'origine (#6873).
--
-- La table 0445 est en ajout seul : elle ne dit pas qu'une preuve a été
-- reconnue fausse. Le 11/11/2026, le bureau a signé avec sa copie de la clé
-- owner trois provenances pour ses propres commits (#6866) ; le retrait de
-- ces preuves n'avait aucun effet mécanique, et un cherry-pick qui aurait
-- ramené les SHA dans un gel les aurait rendues de nouveau utilisables.
--
-- Une révocation est un fait séparé, daté, avec son acteur et sa raison :
-- la ligne de preuve n'est jamais réécrite ni supprimée (0445 garde sa
-- première écriture), et la porte la lit comme une origine inconnue. Seuls
-- les commits sont révocables par ce geste : une preuve de jalon répond
-- d'un verdict déjà rendu, pas d'un commit re-signé.
--
-- La révocation est définitive : une seconde écriture pour le même sujet ne
-- la remplace pas (ON CONFLICT DO NOTHING), et aucune opération courante ne
-- la met à jour ni ne la supprime. Le geste est tracé par `ameesh provenance
-- revoke`, qui exige une identité liée (L244).

create table if not exists provenance_revocations (
    subject text primary key
            references provenance_receipts(subject) on delete restrict,
    sha     text not null,
    actor   text not null check (length(actor) between 1 and 200),
    reason  text not null check (length(reason) between 1 and 2000),
    at      timestamptz not null default now(),
    constraint provenance_revocations_subject_ck check (
        subject ~ '^commit:[0-9a-f]{40}([0-9a-f]{24})?$'),
    constraint provenance_revocations_sha_ck check (
        sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$')
);

create index if not exists provenance_revocations_sha_idx
    on provenance_revocations (sha);

comment on table provenance_revocations is
  '#6873 : retrait daté, motivé et définitif d''une preuve de commit (0445) ; la porte lit une preuve révoquée comme une origine inconnue, et la ligne de preuve reste intacte pour l''audit.';
