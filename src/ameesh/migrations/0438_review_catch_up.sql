-- L539 (#1104) : la borne d'activation du rattrapage des relectures après
-- fusion (`review_lot.catch_up`).
--
-- Une seule ligne : elle est écrite ici, à l'application de cette migration,
-- donc à l'activation du lot sur la base commune. Le rattrapage ne remonte
-- jamais avant elle : un lot déjà fusionné avant l'activation ne devient pas
-- une relecture due aujourd'hui (arbitrage pilote du 15/10 sur le P2 de la
-- R2 #1104). La ligne est lue par tous les exécuteurs et conservée après
-- redémarrage ; un rattrapage historique reste une décision du pilote, qui
-- peut alors déplacer la borne (`UPDATE review_catch_up SET bound_at = ...`).
CREATE TABLE IF NOT EXISTS review_catch_up (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    bound_at timestamp with time zone NOT NULL,
    actor text NOT NULL DEFAULT ''
);

INSERT INTO review_catch_up (id, bound_at, actor)
VALUES (true, now(), 'migration 0438')
ON CONFLICT (id) DO NOTHING;
