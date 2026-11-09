# #5495 — dossier C3 v2.5 : SHA de la relève = SHA actif de la dernière coupure

Auteur : ameesh-dev3 (deepseek), lot **#6532**, 09/11/2026. Delta à `DOSSIER-C3-v24.md`
(conservé ; les scripts et témoins v2.4 ne changent pas). **AUCUN GESTE.** Décision du pilote
`mesh-design` #205738 (09/11 18:54Z), en réponse à #205730.

## 0. Ce que le pilote tranche

- **Fenêtre** du 10/11 à 12:30Z : d'accord sur le principe, confirmée avec le feu. Elle ne doit
  chevaucher ni la bascule REVOKE de #6372 si elle glisse au 10/11 (hors 09:40–11:30Z), ni
  l'activation de #6002 ; le pilote ordonne au moment du feu.
- **Drain d'AG2 et déplacement du bail `mesh-design` sur AG1** : acceptés (« la gouvernance élue
  est faite pour ça »). Le tic paquet-horaire suit le rôle sur AG1. **Pas de drain pendant la CI
  d'un paquet si le tic doit composer ou fusionner** : regarder le porteur avant.
- **SHA : PAS 71cabb7d271e.** D'ici la fenêtre, les coupures de 22:00Z et de 10:00Z auront
  installé une version plus récente ; resoumettre 1.46.0 ferait **redescendre** le staging, ce
  que le canon §11 bis interdit (« la coupure suivante ne redescend jamais sous le commit
  déployé »). Prendre le **SHA actif au moment de la fenêtre**, celui de l'attestation
  `actif-*.json` de la dernière coupure ; le fixer au **gel d'exécution**. La relève reste la
  seule variable, sans changement de code.
- **Si la dernière coupure n'est pas attestée saine sur AG1, AG2 et AG3 : pas de fenêtre.**
- **Feu** après les deux avis OK sur le gel d'exécution, et après lecture de l'état des hôtes à
  ce moment-là. Gestes par le bureau.

## 1. Remplacements dans v2.4

Les passages suivants de `DOSSIER-C3-v24.md` sont remplacés ; le reste (options, critères C1-C6,
retour, preuves, gestes bureau) est inchangé.

| v2.4 | v2.5 |
|---|---|
| §2.1 « Le SHA est 71cabb7d271e… (1.46.0)… Le plan retient le même SHA » | Le SHA n'est **pas** fixé par le plan. À T-15 de la fenêtre, lire la dernière coupure attestée (rapport de release le plus récent), puis son événement `staging.attested` et son `actif-<sha>.json` : le SHA complet doit être **le même sur AG1, AG2 et AG3**, avec les étapes `installer`, `redemarrer`, `controler`, `sante` OK sur les trois, sans négatif ; le fixer dans le reçu d'exécution (note de gel d'exécution + feu du pilote). Le `--sha` du juge et le `submit` prennent ce SHA. |
| §2.3 « SHA et CI : 71cabb… ; CI verte 77849f0e… » | SHA actif de la dernière coupure ; l'exécuteur lit sa CI (`forge_ci`) et doit la trouver verte ; si la CI n'est pas verte ou le SHA absent du clone, **pas de fenêtre**. Le fichier `actif-<sha>.json` de `HOTE_OUTILS` est copié et daté avant la soumission (le re-déploiement du même SHA réécrit le fichier ; l'événement en base reste). |
| §2.4.5 `submit 71cabb7d271e…` | `submit <SHA_ACTIF>` (même commande, SHA fixé au gel d'exécution). |
| §2.5 `sonde-c3-v22.sh apres --sha 71cabb…` | `--sha <SHA_ACTIF>` ; l'attestation attendue est le **nouvel** événement `staging.attested` de ce SHA, daté dans la fenêtre du run, et le nouveau fichier `actif-<sha>.json` ; l'ancien fichier de la coupure est conservé et son sha256 consigné. |
| §2.8.3 « Même SHA ou tête de main ? » | Tranché par le pilote : **SHA actif de la dernière coupure**, ni 71cabb ni une tête de `main` non attestée. |

## 2. Préconditions ajoutées (au gel d'exécution)

- **Dernière coupure saine sur les trois VM** : l'événement `staging.attested` de la dernière
  coupure porte `hotes` avec `ameesh-agents-1`, `ameesh-agents-2`, `ameesh-agents-3`, chacun
  `actif=<sha>` et les quatre étapes OK ; aucun `non_concluants` négatif. Le fichier
  `actif-<sha>.json` de l'hôte qui le porte concorde (sha256, date, trois hôtes). Sinon : NO-GO.
- **Paquet** : avant le drain, vérifier que le tic paquet-horaire (qui suit le rôle) n'a pas une
  CI de paquet en cours ou une composition/fusion due ; le drain déplace le tic sur AG1. Le feu
  du pilote ordonne #6372/#6002 et la relève.
- **Créneau** : 12:30Z est hors de la fenêtre #6372 09:40–11:30Z ; si #6372 glisse et que la
  fenêtre bouge, re-vérifier. Hors 10:00Z et 22:00Z de coupure, comme v2.4.
- **Gel d'exécution** : au T-15, le SHA actif est lu, consigné (rapport + événement + fichier) et
  figé dans le reçu ; le pilote donne son feu sur ce SHA exact. La présente v2.5 ne fige aucun
  SHA : elle fige la **règle** de lecture.

## 3. Pièces

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v25.md` | Ce delta (règle du SHA actif, contraintes pilote) |
| `DOSSIER-C3-v24.md` et pièces v2.4 | Conservés ; les scripts `fenetre-release.sh`, `temoin-fenetre-release.sh`, `sonde-c3-v22.sh` (option `--release-fermee`) ne changent pas |
| `MANIFEST.json` | sha256 et delta v2.4 → v2.5 |
