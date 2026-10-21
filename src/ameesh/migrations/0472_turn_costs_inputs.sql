-- #288 (L80b) : les entrées servies par un tour résident, sur la ligne du grand livre.
--
-- Le coût d'un tour est partagé par les messages qu'il a servis : le tour peut
-- en grouper plusieurs dans sa consigne, ou en recevoir en steer. On garde
-- leurs identifiants (mail id / human_message id) sur la ligne `turn_costs`
-- pour rapporter le coût par message sans compter le tour deux fois.
-- Additif : les lignes antérieures restent NULL (aucun backfill).
alter table turn_costs
    add column if not exists inputs jsonb;
comment on column turn_costs.inputs is
    'L80b : identifiants des messages servis par le tour (JSON array), NULL si non renseigné';
create index if not exists turn_costs_inputs_gin
    on turn_costs using gin (inputs jsonb_path_ops);
