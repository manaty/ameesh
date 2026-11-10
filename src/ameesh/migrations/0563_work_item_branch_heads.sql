-- SPDX-License-Identifier: AGPL-3.0-only
-- 0563_work_item_branch_heads — #6626 (manaty/ameesh#5065) : historique des
-- têtes observées de la branche d'un lot.
--
-- `work_items.branch_head` ne garde que la dernière tête relevée. Un enfant
-- parti d'une tête intermédiaire de son parent, jamais gelée ni relue, n'était
-- plus vu empilé (`stacked-undeclared`, #6607) dès que le parent avançait :
-- cette tête n'était plus ni `branch_head`, ni un gel, ni un verdict. Le
-- relevé des fusions y inscrit désormais chaque tête propre qu'il retient ;
-- `paquet-candidats` et le débit la comptent parmi les têtes du parent.
--
-- Additif seulement : une table neuve, aucune ligne existante modifiée. N-1 :
-- un exécuteur resté sur la version précédente n'y écrit pas (faux négatif,
-- comme avant), sans erreur.
create table if not exists work_item_branch_heads (
    work_item_id bigint not null references work_items(id) on delete cascade,
    branch text not null,
    sha text not null,
    observed_at timestamptz not null default clock_timestamp(),
    primary key (work_item_id, branch, sha)
);
