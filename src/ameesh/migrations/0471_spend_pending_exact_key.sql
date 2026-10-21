-- 0471_spend_pending_exact_key — #2166 : identité exacte du marqueur comptable
--
-- `clear_if` et `set_resident_usage` comparaient l'instant du marqueur relu en
-- `extract(epoch from created_at)::float8` à celui du tour. Une connexion qui
-- imprime le float8 à précision réduite (`extra_float_digits=0`, pools,
-- production) perd les derniers chiffres : la comparaison échouait et le
-- marqueur restait APRÈS le règlement du tour.
--
-- Le marqueur porte désormais sa clé exacte — agent, index de début et
-- `created_at` UTC à la microseconde — écrite avec lui (elle sert aussi de
-- `spend_key` au grand livre et à la réservation #1469). NULL sur les
-- marqueurs d'avant cette migration : leur réparation garde la clé
-- historique dérivée du float8, ce qui dédoublonne la ligne déjà écrite et
-- règle la réservation existante.
alter table spend_pending add column if not exists spend_key text;
comment on column spend_pending.spend_key is
    '#2166 : clé exacte du marqueur comptable (agent:index:created_at UTC), NULL avant 0471';
