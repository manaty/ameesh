# Revue du delta de déduplication des jauges

**NO-GO : 1 P2 bloquant dans la classe sensible ameesh**, sur
`a66546beeefa8eaad8f3e491a65a87c86ce70427`, PR873, delta depuis
`84bec820b0fa136566939fe5efb3dbee2ff28889`.
Auditeur, Codex réellement exécuté, le 14 octobre 2026.

Les quatre constats de la première revue sont corrigés. Un cas supplémentaire
introduit par la nouvelle déduplication peut encore faire admettre un compte
au-dessus de son plafond depuis l'historique partagé.

## P2 : une ancienne valeur masque la dernière à production égale

`src/ameesh/storage/postgres/operations.py:377–386` déduplique désormais par
existence de **n'importe quelle** ligne récente de mêmes valeur/fenêtre et de
production supérieure **ou égale**. À production égale, cette ligne peut
précéder une autre valeur qui est devenue la mesure retenue par `gauge_latest`.

La production est le timestamp du marqueur de tour, écrit par
`account_turn.mark` juste avant le tour : plusieurs `rate_limit_event` du même
tour portent donc le même `produced_at`. Les lecteurs de journal gardent bien
le dernier événement quand ces timestamps sont égaux (`>=` dans le classement).

Témoin, même compte/clé/reset, trois observations sous le même marqueur P :

1. 87 % @ P est inscrit ;
2. 67 % @ P est inscrit après ;
3. 87 % @ P est produit et présenté à `record_gauges` après la deuxième ligne.

Le nouveau prédicat trouve la première ligne 87 % @ P et refuse la troisième.
`gauge_latest`, qui départage les productions égales par insertion, garde alors
67 %. Le journal complet garde 87 % et refuse l'admission sous un plafond de
80 %, mais un état qui se réconcilie depuis l'historique admet le compte à 67 %.

La sonde exécute le SELECT exact extrait du nouvel INSERT sur un CTE VALUES à
horloge fixe, en transaction READ ONLY : **0 candidat**. Le même SELECT extrait
de `84bec820` donne **1 candidat**, confirmant la régression du delta. Le SELECT
réel de `gauge_latest` retient 67 % ; le lecteur et l'évaluation d'admission
réels confirment la divergence locale/distante.

Le cas non daté régresse également : NULL, 67→87→67, dernière 67 supprimée et
87 conservé. Les jauges sans provenance, dont Codex, utilisent ce chemin.

Correction proposée au binôme avec l'auteur : conserver la priorité de
production **strictement supérieure** pour neutraliser les relectures plus
anciennes ; à production égale (NULL inclus), comparer la candidate à la
dernière valeur de ce rang, dans le même ordre que `gauge_latest`, plutôt qu'à
une ancienne valeur identique. Préserver les cas corrigés : nouvelle production
à valeur égale, NULL→date, et rejeu identique plus ancien dédupliqué.

## Constats précédents levés

- Le rejeu postérieur du même journal ne masque plus la mesure fraîche, avec
  ou sans compte ; une vraie baisse datée plus récente reste admise.
- La production plus récente à valeur égale est conservée ; la répétition
  d'une production plus ancienne identique reste dédupliquée ; NULL→date est
  conservé. Le blocage ci-dessus concerne les égalités, absentes de ces témoins.
- `operations.gauge_latest` est explicitement refusée au contrat ;
  `Contract.get` la rejette et `interface_total` passe de 385 à 386.
- `test_1132_jauges_perimees` est présent une fois, commenté et trié dans le
  pilote psql. Les drapeaux d'overage sont également conservés sans compte.

Les neuf omissions d'interface déjà présentes dans la base restent hors delta.
Le compte réel est 395 contre 386 déclarés sur cette tête ; ce n'est pas un
nouveau défaut de classement de #1132.

## Vérification

```sh
PYTHONPATH=src python docs/review/proof-1132-delta-auditeur.py
```

La réussite de cette sonde signifie que les quatre corrections sont vérifiées
**et que le nouveau défaut est reproduit**. Journaux synthétiques, profil sans
identifiants, horloge fixe, SQL capturé puis exécuté seulement sur VALUES,
transaction READ ONLY et timeout de cinq secondes par requête. Aucune table de
jauges lue ou écrite, aucun budget ou quota modifié, aucune migration exécutée,
aucune suite auteur rejouée.

Le second avis Claude, le rebase après #1078 sur sa tête réelle, la CI applicable
et le feu pilote restent distincts. Le constat a été transmis à l'auteur et à
codex3 pour travailler ensemble sur le prédicat, sans multiplier les tours de
relecture ni créer un second correctif produit.
