-- #8340 (manaty/ameesh#6580) : commit complet refusé par un module.
--
-- `last_good_commit` ne suit QUE le dernier contrôle `ok` : un refus de
-- module ne doit jamais y être écrit, sinon un commit refusé passerait pour
-- audité (`canon_confirm`, `audited_commit`, la porte du train). Cette
-- colonne garde donc à part la révision exacte que le module a inspectée et
-- refusée ; NULL quand le module n'a pas épinglé de commit.
--
-- Elle suit la même règle de provenance que `status` : un contrôle `ok`
-- l'efface (le refus est remplacé), un contrôle `invalid`/`unreadable`
-- ultérieur la remplace. Aucune avance de `last_good_commit` n'est touchée.
ALTER TABLE canon_state
    ADD COLUMN IF NOT EXISTS refused_commit text;

COMMENT ON COLUMN canon_state.refused_commit IS
    '#8340 : commit complet refusé par un module (NULL : refus non épinglé) ; effacé par un état ok.';

-- N-1 (#8340, verdict B) : un écrivain resté sur la version précédente ne
-- connaît pas la colonne ; son `ok` gagnant laisserait le refus en place alors
-- que le statut, le diagnostic et le commit audité deviennent ceux du `ok`.
-- L'invariant « un état ok n'a pas de commit refusé » est donc tenu au
-- schéma, pour tous les écrivains : le déclencheur efface la colonne quand la
-- ligne écrite est `ok`. Il est compatible avec le code précédent, qui ignore
-- cette colonne ; un `ok` qui perd le CAS garde le statut non-`ok` du gagnant
-- et n'efface donc rien.
-- ameesh: trigger compatible with previous code (il n'efface qu'une colonne
-- que le code précédent ignore).
create or replace function canon_state_guard_refused_commit() returns trigger
language plpgsql as $$
begin
    if new.status = 'ok' then
        new.refused_commit := null;
    end if;
    return new;
end $$;

drop trigger if exists canon_state_guard_refused_commit on canon_state;
create trigger canon_state_guard_refused_commit
before insert or update on canon_state
for each row
execute function canon_state_guard_refused_commit();
