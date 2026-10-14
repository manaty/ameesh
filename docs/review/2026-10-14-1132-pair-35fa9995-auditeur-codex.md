# Validation du prédicat de déduplication en binôme

**GO delta, 0 B / 0 N nouveau**, sur
`35fa99950` (préfixe de la tête PR873), depuis
`a66546beeefa8eaad8f3e491a65a87c86ce70427`.
Auditeur, Codex réellement exécuté, le 14 octobre 2026.

L'auteur a appliqué le prédicat proposé dans le constat de production égale.
Cette vérification du correctif convenu termine le binôme, sans autre série
d'allers-retours. Aucun correctif produit concurrent n'a été écrit par l'auditeur.

La déduplication distingue maintenant deux cas : une production strictement
plus récente de mêmes valeur/fenêtre peut neutraliser la candidate plus ancienne ;
à production égale, NULL inclus, seule la dernière valeur de ce rang décide.
La sous-requête départage les observations égales par `id`, comme `gauge_latest`.

La sonde indépendante vérifie, avec le SQL exact capturé de `record_gauges` :

- 87→67→87 sous le même marqueur donne un candidat pour la dernière 87 et
  l'historique retient 87 %. L'admission locale et celle reconstruite depuis
  l'historique refusent toutes deux le compte au plafond de 80 %.
- NULL, 67→87→67 conserve la dernière 67 ; sa répétition est dédupliquée.
- Production **et observation** égales conservent aussi la dernière valeur,
  par l'ordre des identifiants ; sa répétition reste dédupliquée.
- Les témoins R1 restent satisfaits : production neuve à valeur égale
  conservée, rejeu identique ancien dédupliqué, NULL→date conservé.
- Le rejeu postérieur ne masque pas la mesure fraîche du journal, avec ou
  sans compte ; une vraie baisse datée est admise et les drapeaux d'overage
  sans compte sont conservés.
- `operations.gauge_latest` reste explicitement refusée au contrat, et le
  module PostgreSQL apparaît une fois, trié, dans le pilote psql.

```sh
PYTHONPATH=src python docs/review/proof-1132-pair-auditeur.py
```

Sonde réussie. Journaux synthétiques, profil sans identifiants, horloge fixe,
CTE VALUES en transaction READ ONLY, timeout de cinq secondes par requête.
L'INSERT n'est jamais exécuté : son SELECT et les deux CTE sont capturés,
puis évalués sans tables réelles. Aucun budget, quota ou historique modifié,
aucune migration exécutée, aucune suite auteur rejouée.

Les neuf omissions d'interface présentes avant #1132 restent hors delta.
Le GO couvre ce contenu convenu ; le second avis Claude réel, le rebase après
#1078 sur sa tête effective, la CI applicable et le feu pilote restent distincts.
