# #5495 — dossier C3 v2.4 : fenêtre de release et relève L2 hors coupure

Auteur : ameesh-dev3 (deepseek), lot **#6532**, 09/11/2026. Base : `review/bureau-5495-c3`
(v1 à v2.3 du bureau, conservées). Branche de ce delta : `review/bureau-5495-c3-v24`.
**AUCUN GESTE.** Les seuls gestes d'hôte de ce plan sont portés par le bureau, sur feu du
pilote et après deux avis de familles distinctes. Aucune action irréversible, aucune production,
aucune fusion, aucune suppression de données.

Sources : lot #6532 (mesh-design), notes et événements du lot #5495, dossiers C3 v1 à v2.3,
défaut #203817 (09/11 09:45Z, bureau), #190531 (pas de relève forcée), coupure 1.46.0 du 09/11
(événements `staging.request` 606388/606397/609129, `staging.attested` 609127,
`staging.sequence` 606403 → 609128, run `adf3e26d242a4cc2b9879784d5241340`).

État de départ, 09/11 :
- L2 est posé et actif sur AG1 et AG2 (C3 : reçu #203627, activation #203834), minuterie
  `ameesh-staging.timer` `enabled`/`active` sur les deux ; `staging-executor.json` présent sur
  les deux, `role=mesh-design`, `candidates=[ameesh-agents-1, ameesh-agents-2]`, `key=mesh-staging`.
- Le bail `mesh-design` est tenu par AG2 (époque 397 au relevé de 18:50Z, propriétaire
  `runner-mesh-design@ameesh-agents-2`) ; AG1 est candidat rang 0, prêt et frais.
- La coupure 1.46.0 a été portée de bout en bout par L2 sur AG2 : demande 10:03:34Z, exécution
  10:04:18Z, fin 12:02:52Z, attestation `actif-71cabb7d271e.json` (12:02:45Z, 3 hôtes, 2 reportés).
- Canon des agents et `canon_ref` de L2 : `b8967b85f7219f8b8d15eff72fb401912fafba89` (relevé
  09/11 18:5xZ, identique des deux côtés).

## 1. Défaut de séquence #203817 (09/11 09:45Z)

### 1.1 Fait

Le 09/11 à 09:45Z (T-15 de la coupure 10:00Z), un **tick de release** était en cours sur le
candidat ; la séquence ne pouvait pas lancer la sonde `go` dans l'état voulu. Le bureau n'a pas
activé dans cet état : il a attendu un instant « hors tick », a lancé la sonde `go` de 09:51:03
à 09:51:50Z (OK), puis l'activation de 09:51:58 à 09:52:08Z. La coupure de 10:00Z est ensuite
partie normalement. Le fait est rapporté par le bureau (#203817, cité par le lot #6532) et par
la note de lot #5495 de 09:52Z.

Le défaut n'est pas la sonde : c'est **l'ordre de la séquence**. La fenêtre de release n'est
fermée qu'*après* la sonde `go` et *avant* l'activation ; entre la sonde et la fermeture, un tick
peut démarrer, et la sonde ne fait que constater un instant sans tick. On dépend donc d'une
course, qui a coûté six minutes sur une butée de quinze ; elle peut aussi laisser un tick
démarrer entre la sonde et le retrait du drop-in `10-sans-staging.conf`, qui change précisément
le comportement du tick. La séquence doit **fermer la fenêtre d'abord, puis sonder**.

### 1.2 Correctif (v2.4)

Avant la sonde `go` (et avant toute activation), sur **chaque** candidat, dans cet ordre :

1. relever et garder l'état de `ameesh-release.timer` (`UnitFileState`, `ActiveState`) ;
2. arrêter la minuterie (`stop`) ;
3. attendre, borne 120 s, que `ameesh-release.service` soit `inactive` **et** qu'aucun job
   `ameesh-release` ne reste en file ; au-delà de la borne : **STOP**, pas de reprise ;
4. lancer la sonde `go` **fenêtre fermée** : la case « minuterie release active » devient
   « minuterie release arrêtée par cette séquence, état gardé » (option `--release-fermee`
   ou mode dédié ; toutes les autres cases, dont « aucun tick » et « aucun job », restent) ;
5. garder la minuterie arrêtée pendant l'activation (soumission, minuterie L2, retrait du
   drop-in) ; ne pas l'arrêter une seconde fois ;
6. restaurer la minuterie après l'activation ; `start` puis `ActiveState=active` exigé si elle
   était active. État gardé absent, vide ou inconnu : appliquer la règle v2.3 (relance explicite
   vérifiée si `UnitFileState=enabled`, sinon STOP avant tout arrêt).

Commandes de référence (bureau, root, gestionnaire `ameesh@`, par candidat) :

La référence exécutable est `fenetre-release.sh` (§3) ; l'essentiel, par candidat :

```sh
./fenetre-release.sh --apply fermer          # état gardé, stop, attente bornée 120 s
./sonde-c3-v22.sh go --release-fermee <TS>   # la case « minuterie release » lit l'état gardé
# ... activation (soumission, minuterie L2, retrait du drop-in) ...
./fenetre-release.sh --apply restaurer <TS>  # start + ActiveState=active exigé
```

Le script lit `list-jobs` à part (un échec de lecture n'est jamais un « 0 job ») et n'arrête la
minuterie qu'après avoir relevé et écrit l'état des **deux** hôtes.

### 1.3 Portée

Le C3 du 09/11 est déjà activé : cette section est le **constat** et la règle pour la prochaine
séquence d'hôte. Le plan de relève du §2 l'applique (il n'y a pas de sonde `go` de C3 à rejouer,
mais la fenêtre de release doit être fermée avant le drain et rouverte après). Les scripts
v2.2/v2.3 ne sont pas réécrits, sauf le delta `--release-fermee` de `sonde-c3-v22.sh` (v2.4) ;
le reste est conservé.

**Limite v2.2 conservée.** La sonde `go` vérifie `actif-<sha>.json` de la dernière release sur
**AG1** seulement. Au 09/11, l'attestation de 1.46.0 est sur AG2 (`HOTE_OUTILS`), pas sur AG1 :
une future C3 devra lire les trois VM (ou l'hôte qui a lancé la séquence), pas AG1 en dur. Ce
point est hors du delta v2.4 (la relève ne rejoue pas `go`) ; il est noté pour la prochaine C3.

## 2. Qualification réelle de la relève L2 AG2 → AG1, hors coupure

### 2.1 Objet et critères d'acceptation

Rendre le détenteur AG2 indisponible par un **drain planifié** ; AG1 prend le bail `mesh-design`
avec une **nouvelle époque** ; son exécuteur L2 porte une **soumission de SHA en staging** ; une
seule exécution (pas de double) ; puis retour à l'état nominal. Preuve : les deux époques,
l'attestation et le rapport.

| # | Critère | Preuve attendue |
|---|---|---|
| C1 | Avant : AG2 détenteur, époque E0 ; L2 actif sur les deux ; aucune demande en cours ; fenêtre de release fermée | `role status` E0, `staging.request` absent/terminal, `staging_watch.in_flight` vide, état des minuteries release |
| C2 | AG2 drainé ; AG1 détenteur, époque E1 > E0, `lease_owner=runner-mesh-design@ameesh-agents-1` ; AG2 n'est plus détenteur | `role status` E1, ligne `host_lifecycle` AG2 (mode `draining`), `agent.updated` `leased:false` puis `true` |
| C3 | Une soumission sur AG1 : une demande `pending`, puis `running` avec `host=ameesh-agents-1`, `run=R`, `started>=E1` | JSON de `submit`, événements `staging.request` et `staging.sequence` de R |
| C4 | Pas de double exécution : un seul run R pour cette demande ; AG2 en standby (« another role holder ») et aucun départ de séquence sur AG2 | `evaluer-c3.py` (un seul run), journal L2 d'AG2, événements `staging.sequence` (un seul départ) |
| C5 | Le run se termine (sortie 0) et une attestation `staging.attested` du SHA est écrite dans la fenêtre du run | événement `staging.attested`, fichier `actif-<sha>.json` (nouveau), `evaluer-c3.py` |
| C6 | Nominal : AG2 rendu (`undrain`), minuteries L2 actives sur les deux, minuteries release restaurées actives, un seul détenteur (AG1), aucune demande en attente | `host_lifecycle` AG2, `role status` final, `systemctl show` des minuteries |

Le SHA est **71cabb7d271e417f3ca02272c46b6a711de3d938** (1.46.0), déjà actif en staging : la
soumission le re-déploie sans contenu nouveau ; l'API de CI de l'exécuteur le connaît déjà
(`forge_ci` sur la chaîne de release, CI verte `77849f0e82bc`). Variante possible, à trancher par
le pilote : la tête de `main` du moment (65c82625b au relevé), plus récente mais non encore
attestée en staging. Le plan retient le même SHA, le plus sûr.

### 2.2 Options de relève

| Option | Pour | Contre | Verdict |
|---|---|---|---|
| **(a) Drain planifié d'AG2** (`ameesh host drain ameesh-agents-2 --for 30m`) | Chemin prévu : `storage/postgres/host_lifecycle._release_elected` libère immédiatement le bail élu (époque + 1), sans attendre le TTL ; `--for` rend la main automatiquement ; aucun SQL, aucun arrêt du processus du pilote | Les ~20 agents d'AG2 ne prennent plus de tour pendant le drain (quelques minutes ; on `undrain` dès que la demande est prise) | **Retenu** |
| (b) Arrêter `ameesh-runner-agent@mesh-design` sur AG2 | Plus léger pour les autres agents | Tue le runner du pilote (un tour en cours peut être perdu) ; le bail attend le TTL ; ce n'est pas le chemin documenté de retrait | Rejeté |
| (c) Forcer l'époque en SQL | Immédiat | Interdit (#190531 : pas de relève forcée, pas de SQL ; canon : gestes d'hôte par le bureau, jamais de contournement de l'élection) | Rejeté |
| (d) Attendre une élection naturelle | Aucun geste | Non déterministe ; ne qualifie rien | Rejeté |

Le drain libère le bail élu dans la même transaction que la fermeture des prises ; AG1 réclame à
sa passe d'élection (rang 0, délai 0) avec une nouvelle époque, l'ancien détenteur est fencé par
l'époque (EP4). Aucun `agent-runner register`, aucun déplacement manuel.

### 2.3 Préalables (à relever avant le gel de la fenêtre)

- Aucune coupure dans la fenêtre (pas de 10:00Z ni de 22:00Z en cours ou imminent).
- `ameesh-release.timer` : `enabled`/`active` sur AG1 et AG2 ; `ameesh-release.service` inactive ;
  aucun job `ameesh-release`.
- Aucune demande `mesh-staging` `pending`/`running` ; `staging_watch.in_flight` vide ; aucune
  séquence `staging.sequence` `running`.
- AG1 et AG2 : `ameesh-staging.timer` `enabled`/`active`, service `inactive`,
  `staging-executor.json` = `/etc/ameesh/staging-submit.json`, `expected_sha256` vérifié,
  `canon` 980 = `canon_ref` = canon des agents (`b8967b85…` au relevé ; à re-vérifier au gel).
- AG1 : candidat `ready`, `fresh`, rang 0, admis ; disque ≥ 10 GiB libre ; pas de hold d'hôte.
- AG2 : candidat `ready`, `fresh`, détenteur E0 ; `mesh-design` **au repos** (`status=idle`,
  aucun tour en cours) ; l'état de la minuterie release d'AG2 est relevé comme celui d'AG1.
- SHA et CI : 71cabb7d271e… ; CI verte lue par l'exécuteur ; le fichier
  `actif-71cabb7d271e.json` de `HOTE_OUTILS` est **copié et daté** avant la soumission (son
  sha256 est consigné), pour ne pas perdre la preuve complète de la coupure du 09/11.
- Relecture : deux avis de familles distinctes sur le présent dossier, gelé ; feu du pilote pour
  la fenêtre ; bureau prêt et joignable.

### 2.4 Séquence (bureau, au premier refus nommé : STOP et retour §2.6)

0. **Fenêtre de release fermée** sur AG1 et AG2 (§1.2), état gardé, tick et jobs attendus ; la
   minuterie reste arrêtée jusqu'au point 8.
1. **E0** : `ameesh role status mesh-design --json` → détenteur, époque, propriétaire ;
   `host_lifecycle` d'AG2 ; ligne `agent_registry` de `mesh-design`. Copier les trois JSON,
   les horodater (`date -u +%FT%TZ`) et consigner leur sha256.
2. **Drain d'AG2** : `ameesh host drain ameesh-agents-2 --for 30m` (identité du bureau ; si sa
   session n'est pas admise comme correcteur, `--actor human:smichea` avec la clé de l'owner,
   comme pour les autres gestes de déploiement). Consigner le retour (`mode=draining`,
   génération, `return_at`).
3. **Attente de la nouvelle époque**, borne 120 s : `ameesh role status mesh-design --json`
   doit donner `holder=ameesh-agents-1`, `epoch=E1>E0`,
   `lease_owner=runner-mesh-design@ameesh-agents-1`. Consigner E1. Échec : STOP, `undrain`
   d'AG2, restauration des minuteries release, rapport.
4. **AG2 hors bail** : vérifier `holder!=ameesh-agents-2` ; au tick suivant (≤90 s), le journal
   L2 d'AG2 (gestionnaire `ameesh-staging@`) doit dire `standby` / « another role holder »,
   sans départ de séquence. Consigner.
5. **Soumission** (sous l'uid 980, sur AG1), dans l'environnement de l'unité :

   ```sh
   ssh -F none -o BatchMode=yes root@78.232.56.22 \
     'runuser -u ameesh-staging -- env HOME=/home/ameesh-staging sh -s' <<'SH'
   set -eu
   set -a; . "$HOME/.config/ameesh/env"; set +a
   exec "$HOME/.local/share/ameesh/venv/bin/python" -m ameesh.staging_executor \
     --config "$HOME/.config/ameesh/staging-executor.json" submit \
     71cabb7d271e417f3ca02272c46b6a711de3d938
   SH
   ```

   Consigner le JSON rendu (`request`, `sha`, `state=pending`) ; aucun secret n'est affiché.
6. **Prise par L2 AG1**, borne 90 s : la demande passe `running` avec `host=ameesh-agents-1`,
   `run=R`, `started>=E1` ; l'événement `staging.sequence` de R démarre. Consigner R et l'heure.
   Échec : ne pas lancer un second exécuteur ; STOP, consigner, `undrain` et restauration.
7. **Retour d'AG2** : `ameesh host undrain ameesh-agents-2` ; consigner le retour ; vérifier
   qu'AG1 reste détenteur et qu'AG2 est candidat en standby.
8. **Restauration de la fenêtre de release** sur AG1 et AG2 (§1.2 point 6) ; `ActiveState=active`
   exigé. Consigner.
9. **Reçu** : le bureau écrit dans `/root/ameesh-operator-C3-releve-<TS>/` les captures des
   points 1 à 8 et le résumé ; il les remet au lot #6532. La séquence de staging continue ; le
   bureau n'y touche plus.

### 2.5 Preuves et jugement

- **Les deux époques** : E0 dans `role-status-E0.json` (AG2) et E1 dans `role-status-E1.json`
  (AG1), avec `lease_owner`, horodatage et sha256 ; la ligne `host_lifecycle` d'AG2 et les
  événements `agent.updated` (`leased:false` au drain, `leased:true` à la réclamation).
- **La soumission et le run** : le JSON de `submit`, les événements `staging.request` et
  `staging.sequence` (un seul run R, hôte AG1).
- **L'attestation** : l'événement `staging.attested` du SHA, daté dans la fenêtre du run, et le
  nouveau fichier `actif-71cabb7d271e.json` ; l'ancien fichier conservé et son sha256.
- **Le jugement** : `sonde-c3-v22.sh apres --sha 71cabb… --depuis <horodatage Unix du submit>
  --canon b8967b85…` (N7 + `etat actif` + `evaluer-c3.py`). E0 et E1 sont des **époques de
  bail**, pas des horodatages ; `--depuis` prend le temps Unix (secondes) du submit. Le juge
  doit rendre :
  un seul run, `completed`, `host=ameesh-agents-1`, `canon_ref` = canon, empreintes =
  `expected_sha256`, attestation dans la fenêtre, drains `ameesh:staging` au principal owner.
- **Pas de double exécution** : un seul run, un seul départ de `staging.sequence`, journal AG2
  en standby, et le bail d'exécution partagé `staging-execution:mesh-staging` (pris par le
  `tick` d'AG1 ; `submit` le rend avant).
- **Le rapport** : note au lot #6532 (3 lignes de synthèse + liens SHA) et, si l'auditeur en
  donne mandat, son attestation sur les pièces ci-dessus.

### 2.6 Retour et échecs

- **Avant le drain** : rien à défaire, sauf la fenêtre de release (la restaurer).
- **Après le drain, avant la soumission** : `undrain` d'AG2, restauration de la fenêtre ; le bail
  peut déjà être sur AG1 (E1) : c'est un état nominal valide, le consigner.
- **Après la soumission** : ne pas annuler le run ; `undrain` d'AG2, restauration de la fenêtre ;
  le run suit son cours (l'exécuteur gère l'échec et le retour) ; rapport.
- **Run en échec** : le takeover (C1-C4) peut rester prouvé, mais C5 manque ; ouvrir un lot
  correctif, aucun rafistolage d'hôte dans la fenêtre.
- **Jamais** : tuer le service L2 pendant une séquence, arrêter le runner du pilote, écrire en
  SQL, forcer une époque, supprimer des données, toucher la production.

### 2.7 Fenêtre proposée

**2026-11-10 12:30Z** (après la coupure de 10:00Z, avant celle de 22:00Z ; le staging de 1.46.0
a duré 1 h 59, donc une fin vers 14:30 laisse 7 h 30 avant la coupure suivante). À confirmer
par le pilote et le bureau ; si la coupure de 10:00Z déborde, glisser après elle. Aucune
exécution pendant une coupure.

### 2.8 Points ouverts (relecteurs et pilote)

1. Le drain d'AG2 (agents en pause quelques minutes) est-il acceptable, ou faut-il une fenêtre
   plus courte / un autre candidat ?
2. Le pilote accepte-t-il que le bail `mesh-design` reste sur AG1 après la relève ? L'élection ne
   le rend pas à AG2 sans un second drain (hystérésis) ; un retour forcé serait une autre
   opération, hors de ce lot.
3. Même SHA (re-déploiement sans contenu) ou tête de `main` du moment ? Le plan recommande le
   même SHA.
4. L'auditeur atteste-t-il les pièces du §2.5 ?
5. Le delta `--release-fermee` est une option de la sonde (pas une copie `sonde-c3-v24.sh`) ;
   un relecteur qui juge le fichier gelé intouchable peut demander une copie, mais l'option
   garde les 108 cases communes et le comportement v2.2 par défaut.

## 3. Pièces v2.4

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v24.md` | Ce dossier (défaut de séquence + plan de relève) |
| `fenetre-release.sh` | Ferme / restaure la fenêtre de release sur AG1 et AG2, état gardé, borne 120 s, STOP au premier refus ; à blanc par défaut |
| `temoin-fenetre-release.sh` | Témoins locaux (faux `systemctl`) : tick actif → attente puis poursuite ; borne dépassée → STOP ; état inconnu → STOP avant tout arrêt ; restauration active/inactive |
| `sonde-c3-v22.sh` | Delta v2.4 : option `--release-fermee TS` (la case « minuterie release » exige alors l'état gardé et `inactive` ; toutes les autres cases inchangées). Sans l'option, comportement v2.2 identique |
| `MANIFEST.json` | sha256 des pièces, delta v2.3 → v2.4 |

Les pièces v1 à v2.3 (scripts, lecteur, juge, témoins, reçus) sont conservées telles quelles, sauf le delta `--release-fermee` de `sonde-c3-v22.sh` listé ci-dessus (le reste du fichier est inchangé).
