-- 0464_spend_pending_resident_usage — L80a : état durable d'un tour résident
-- dont l'événement d'usage n'a pas encore pu être écrit.
--
-- `resident_usage` porte l'événement `resident.turn` (JSON) tant que la ligne
-- du grand livre n'est pas écrite. Un worker de remplacement y retrouve la
-- mesure et peut réparer le tour (écrire l'événement, puis la ligne) sans
-- dépendre de la mémoire du worker qui a servi le tour. La colonne est
-- effacée avec le marqueur ; un nouveau marqueur (put) la remet à NULL.
alter table spend_pending add column if not exists resident_usage jsonb;
comment on column spend_pending.resident_usage is
    'L80a : événement resident.turn d''un tour non réglé (JSON), NULL sinon';
