# #901 — R2 Codex

Gel : `642fd308c94eae2dbdc52e5d227a0a8b13d40db7`, manaty/ameesh PR #703.
**BLOQUÉ : 1 B / 0 N**.

## B1 — les échecs d'une tentative précédente sont imputés à la relance

`src/ameesh/ci.py`, `events_of` et `Watcher._failures` : le filtre ne vérifie
que `execution` (run_id), puis `_failures` attribue tous les événements acceptés
à `row.run_attempt`. Une relance conserve le run_id. Quand elle est queued,
son nouveau job n'a pas encore publié le check custom `tests (flux)` ; l'ancien
check du même nom reste le dernier publié sur le commit. Son ancien échec est
donc enregistré avec run_attempt=2. Même après un passage réussi du test à la
relance, rien ne retire cet échec : l'interface et le verdict peuvent imputer
à l'agent un échec de la tentative 1 et envoyer un urgent indu.

Le témoin ajouté dans `test_relance_rouvre_la_ligne_et_suit_l_attempt` force
`self.api.checks[sha] = []` dès la relance. Cette hypothèse masque précisément
la fenêtre queued avant publication du nouveau job. `scripts/ci-flow.py`
publie des checks custom par POST, et ne marque actuellement pas les événements
avec le numéro de tentative.

Contre-exemple ciblé, sans base ni suite, sur le code gelé :
`PYTHONPATH=src python reviews/901-r2-stale-check-probe.py` — exit 0,
`REPRODUCED: attempt 1 failure inserted as attempt 2 while rerun is queued`.
Le témoin appelle réellement `_failures`, avec une ligne attempt=2 et le check
completed/failure de l'attempt 1, puis constate l'insertion sous attempt=2.

Attendu : distinguer la provenance des événements par tentative (ou une preuve
équivalente fiable), ignorer le check ancien pendant queued, et couvrir ce cas
en conservant les anciens checks dans la simulation. La lecture doit aussi
respecter les jobs non relancés lors de `rerun --failed`.

Autres points relus : migration additive 0429, contrat de colonnes superviseur,
garde SQL run_attempt strictement croissant et non-résurrection des runs
superseded, filtrage des échecs/base par tentative. Diff-check PASS.
Aucune suite complète rejouée (§3). CI exacte 38416345032 sur 642fd308 : pending
au verdict ; aucun vert revendiqué.

Après les deux tours déjà effectués, correction en binôme auteur/relecteur
conformément au §2, sans lancer une nouvelle boucle de relectures.
