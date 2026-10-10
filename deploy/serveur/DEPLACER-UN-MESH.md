# Déplacer un mesh vers son serveur et le séparer (lot L57)

Avant la v2, un même poste a pu faire tourner les personas de **plusieurs
organisations dans une seule base**. La décision 0033 §1 l'interdit : un mesh
n'a qu'une organisation. Cette procédure installe le mesh d'**une**
organisation sur son serveur (profil VM, lot L55). On y copie sa part de la
base partagée, puis ses exécuteurs y sont rebranchés. La base d'origine n'est
pas modifiée : elle sert de retour arrière.

Durée d'arrêt des personas déplacées : 15 à 30 minutes. Aucune n'est coupée
en plein tour.

## En un script

`basculer-mesh.sh` enchaîne les points 1 à 5 depuis le poste, une étape à la
fois (`verifier`, `preparer`, `arreter`, `copier`, `rebrancher`, `demarrer`,
`etat`). Ses paramètres (organisation, dépôts, dossiers, personas déplacées)
sont dans `~/.config/ameesh/bascule.env`, hors du dépôt ; l'en-tête du script
les décrit. La scission s'y fait **sur le poste** : la part des autres
organisations ne quitte jamais le poste, seule celle de l'organisation va sur
la VM. Les sessions interactives (`PERSONAS_ATTACHEES`) ne sont pas relancées :
leur humain les rattache (`ameesh attach`) une fois la bascule faite.

## 0. Préalables

- La VM de l'organisation installée (`README.md`), le poste dans son WireGuard
  (`ajouter-appareil.sh`).
- La même version d'ameesh sur le poste et sur la VM (`ameesh --version`), et
  la même version majeure de Postgres. Sur le poste, `pg_dump` doit pouvoir lire
  la base.
- La liste des **équipes** de l'organisation (`agent_registry.team`) et
  l'identifiant de son **canon** (vide pour le canon par défaut du poste).

## 1. Répétition sur une base d'essai (sans rien arrêter)

```bash
pg_dump -Fp --no-owner --no-privileges "$DSN_DU_POSTE" -f /tmp/mesh.sql
scp /tmp/mesh.sql deploy/serveur/scinder-mesh.sql root@10.77.0.1:/tmp/
ssh root@10.77.0.1
  runuser -u postgres -- createdb -O ameesh ameesh_essai
  runuser -u postgres -- psql -q -d ameesh_essai -v ON_ERROR_STOP=1 -c "set role ameesh" -f /tmp/mesh.sql
  runuser -u postgres -- psql -d ameesh_essai -v equipes='{equipe1,equipe2}' -v canon_garde='' -f /tmp/scinder-mesh.sql
```

Vérifier les comptes affichés (personas, messages, lots, tours), puis qu'il ne
reste **aucun** message vers ou depuis une persona d'une autre organisation.

## 2. Arrêt des personas déplacées, entre deux tours

Pour chaque persona de l'organisation, attendre qu'elle soit entre deux tours
(`ameesh show <persona>` : statut `idle`), puis arrêter son exécuteur
(`systemctl --user stop ameesh-runner-agent@<persona>`). Une persona attachée
(`ameesh attach`) est d'abord rendue par son humain. Les personas des autres
organisations continuent de tourner.

## 3. Copie et séparation

Même chose qu'au point 1, sur la base `ameesh` de la VM (vide jusque-là),
avec un `pg_dump` pris **après** l'arrêt. Puis
`runuser -l ameesh -c "ameesh migrate && ameesh doctor"`.

## 4. Sessions et mémoire

- Les sessions des personas qui tourneront **sur la VM** y sont restaurées
  depuis leur sauvegarde (`ameesh session restore`, lot L53), ou copiées. Une
  session Claude ou DeepSeek est rangée sous un nom tiré du dossier de
  travail : le renommer si ce dossier diffère sur la VM.
- Les personas qui ont un dépôt de mémoire (L54) le reclonent d'elles-mêmes à
  leur prochaine session.

## 5. Rebranchement

- **Sur la VM** : canon de l'organisation (clé de déploiement en lecture
  seule), `canons` dans la configuration de `ameesh`, `ameesh canon sync
  --fetch`, clés API de l'organisation dans `/etc/ameesh/harnais.env`, puis
  `systemctl enable --now ameesh-runner@<persona>` pour chaque persona qui y
  tourne.
- **Sur le poste**, pour les personas qui y restent (abonnements, sessions
  interactives) : une configuration propre au mesh de l'organisation, avec
  `dsn` pointant vers `10.77.0.1` en `sslmode=require` et les seuls canons de
  l'organisation. Les exécuteurs la reçoivent par `AMEESH_CONFIG`, et les
  sessions interactives par le dossier, avec `mise.local.toml` : `[env]
  AMEESH_CONFIG = "…"`.
- Sur le poste, les personas déplacées sont marquées arrêtées dans l'ancienne
  base (`agent-runner stop <persona>`), pour qu'aucun exécuteur ne les y
  réclame.

## Retour arrière

Arrêter les exécuteurs de la VM et remettre ceux du poste sur l'ancienne
configuration (`systemctl --user start ameesh-runner-agent@<persona>`).
L'ancienne base n'a pas été modifiée, à part le marquage d'arrêt, qu'on lève
avec `agent-runner register <persona> … --session …`. Le courrier arrivé
entre-temps sur la VM s'exporte par `ameesh mail inbox <persona>`, puis se
renvoie.
