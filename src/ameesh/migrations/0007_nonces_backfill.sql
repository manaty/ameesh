-- 0007_nonces_backfill — reprendre les consommations historiques dans le ledger.
--
-- 0006 a introduit `mesh_consumed_nonces`, mais une approbation consommée
-- AVANT la migration (colonne `consumed_at` seule) n'y figurait pas : après
-- suppression puis réinsertion du même reçu signé, elle redevenait
-- consommable (sonde codex3 « upgrade-nonce »).
--
-- 0006 reste immuable ; c'est cette migration qui rattrape l'existant. Elle
-- couvre aussi les consommations faites entre 0006 et 0007.
--
-- `on conflict do nothing` : le ledger est déjà la référence, on ne réécrit
-- pas une consommation plus récente.

insert into mesh_consumed_nonces (approver, nonce, approval_id, consumed_by, consumed_at)
select a.approver,
       a.nonce,
       a.id,
       coalesce(a.consumed_by, 'avant-ledger'),
       coalesce(a.consumed_at, now())
  from mesh_approvals a
 where a.consumed_at is not null
on conflict (approver, nonce) do nothing;
