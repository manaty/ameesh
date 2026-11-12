# Bureau sur bureau-goal : CLI ameesh et connexion limitée (#6881)

Source : décision owner #211859 option a (signée, approbation #325, 11/11),
lot #6881 (manaty/ameesh#5236), partie de #6877. Staging seulement (§14).

But : chaque session du bureau sur bureau-goal (BG) se **lie** avec un PID
vérifié (`ameesh mail bind`), **prend** le lot avant tout geste à dépense ou
sur hôte (`ameesh work claim`, #6839), et ses commits portent un **reçu**
(hooks de provenance). Elle le fait avec une connexion PostgreSQL limitée,
jamais avec le rôle `ameesh` ni un DSN d'agent.

## Ce que la connexion peut faire, et rien d'autre

| Geste | Commande | Droits |
|---|---|---|
| lire les lots qu'il exécute | `ameesh work show <lot>` | SELECT des tables de lots, du registre, du courrier ; `actions` limité aux colonnes du résumé (ni `args`, ni empreinte, ni nonce, ni reçu) |
| se lier, PID vérifié localement | `ameesh mail bind|unbind` | INSERT/UPDATE de colonnes de `session_bindings` |
| prendre / lever un geste externe | `ameesh work claim <lot> --session <id> [--release]` | UPDATE des seules colonnes `external_claim_*` de `work_items` (dont `external_claim_harness` et `external_claim_binding` dès la migration 0566 de #6877) |
| reçus de provenance | hooks `provenance intent|created|record` | INSERT de `provenance_receipts` |
| courrier | `agent-mail send`, `agent-mail inbox` | INSERT de `agent_mailbox` ; UPDATE de `meta`, `delivered_at`, `status` (réservation et remise du courrier reçu) |

La liste exacte est `deploy/sql/_bureau-droits.sql` ; `_contrat-bureau.sql`
compare les droits **effectifs** (PUBLIC et appartenances compris) à cette
liste et refuse tout écart.

Les droits de colonne ne bornent pas les **lignes**. La garde de la migration
0567 (`ameesh_limited_login_guard`) s'applique aux seules connexions membres
du rôle marqueur `ameesh_login_limite`, inscrites dans `limited_logins` avec
leur agent et leur hôte. Pour ces connexions, la base refuse :

- tout courrier, liaison, reçu ou note qui n'est pas à leur nom, ou qui vient
  d'un autre hôte ;
- la prise d'un lot qui n'est pas réservé à leur agent (`external_executor`) ;
- une prise sans liaison L41 active de cet agent sur cet hôte, ou qui nomme
  (#6877) un harnais ou une ligne de liaison qui n'est pas celle-là ;
- l'écrasement de la prise d'une autre session ;
- toute écriture du registre (le droit UPDATE n'y sert qu'au verrou de ligne
  que prennent le dépôt et la remise du courrier) ;
- sur le courrier reçu, tout changement autre que la réservation
  (`meta.consigne`) et la remise ; un message remis ne redevient pas non lu ;
- tout événement écrit hors trigger ;
- toute suppression ;
- la réouverture d'une liaison révoquée.

Ce qui reste fermé : DDL (ni CREATE, ni propriété), SECURITY DEFINER, tables
d'approbation (`mesh_approvals`, `standing_approvals`, reçus), clés
(`key_holders`), comptes, secrets, SET ROLE.

Limites, dites franchement :

- la lecture du courrier n'est pas bornée au destinataire, parce que le CLI
  lit le dernier message de chaque agent ;
- un renommage de l'agent `bureau` ne déplace pas `limited_logins.agent` :
  la garde refuse jusqu'à la réinstallation du rôle sous le nouveau nom ;
- deux sessions du même agent sur bureau-goal partagent la connexion : la
  base les distingue par leur liaison L41, et le CLI vérifie le PID
  localement ;
- le hook `agent-mail hook` n'est pas installé sur bureau-goal : son
  inscription écrit le registre, fermé à cette connexion. Sous la connexion
  limitée, il ne fait pas échouer la session et laisse le courrier non lu ;
  le bureau lit par `agent-mail inbox` ;
- la base ne peut pas empêcher une session de lever la prise d'une autre
  session du même agent sur le même hôte. Le CLI la refuse (« verified
  session is … »).

## Côté hub (administrateur, lot suivant de #6877)

1. **Migration 0567** : elle part avec le déploiement staging ordinaire. Elle
   est additive, et la garde est inerte tant qu'aucune connexion limitée
   n'existe.
2. **Pair WireGuard** de bureau-goal, sur le serveur du mesh. Sur le mesh
   manaty, il est posé par `ameesh-bg-peer.service` (`10.77.0.6/32`, filtre
   avant pair, #6269 ; pose : `deploy/serveur/RESEAU-PRIVE.md`, « Pairs hors
   de `wg0.conf` »). Ailleurs : `deploy/serveur/ajouter-appareil.sh
   bureau-goal`, qui attribue une adresse libre `10.77.0.N/32` (côté hub :
   `AllowedIPs = 10.77.0.N/32`) et affiche la configuration de l'appareil. Côté bureau-goal :
   `AllowedIPs = 10.77.0.1/32`, c'est-à-dire la seule adresse du hub, pas le
   /24. La clé privée n'est affichée qu'une fois : elle va directement dans
   `/etc/wireguard/ameesh.conf` (0600) sur bureau-goal.
3. **Port 5432 seulement** depuis ce pair : sur le hub, une règle nftables
   refuse tout autre port venant de `10.77.0.N` sur `wg0`, par exemple
   `iifname "wg0" ip saddr 10.77.0.N tcp dport != 5432 drop` et
   `iifname "wg0" ip saddr 10.77.0.N udp drop`.
4. **pg_hba** : avant la ligne générale `ameesh-mesh`, ajouter
   `hostssl ameesh bureau_goal 10.77.0.N/32 scram-sha-256` puis
   `host all bureau_goal all reject` (seul ce pair, en TLS), et recharger
   PostgreSQL.
5. **Rôle**, par un administrateur (CREATEROLE et propriétaire des tables) :
   ```sh
   PGOPTIONS='-c search_path=public' psql "<DSN admin>" -X -v ON_ERROR_STOP=1 \
     -v role=bureau_goal -v agent=bureau -v host=bureau-goal \
     -f deploy/sql/role-bureau.sql
   ```
   Ensuite, dans psql : `SET password_encryption='scram-sha-256'; \password bureau_goal`.
   Le secret est transmis au bureau par fichier 0600, jamais par courrier ni
   en argument. Après chaque migration, l'audit seul (`_contrat-bureau.sql`,
   même commande) doit passer.
6. **Lot de contrôle** : un lot réservé au bureau, par exemple
   `ameesh work add … --external-executor bureau`. Son numéro sert à l'étape
   `controler`.

## Côté bureau-goal (compte Unix du bureau, jamais root)

```sh
S=deploy/bureau/installer-bureau-goal.sh   # depuis un clone d'ameesh
$S verifier
$S installer v1.49.0                        # version déployée en staging (commit de main)
$S connexion < /chemin/secret-bureau_goal   # fichier 0600 ; ~/.pgpass 0600, config sans secret
$S hooks ~/ameesh ~/canon ~/nexlink         # clones où le bureau commite
$S controler <lot de contrôle>
```

L'étape `controler` doit montrer :

1. une liaison avec le PID vérifié du script ;
2. la prise du lot de contrôle, puis le refus d'une seconde session liée
   (« already taken by … ») ;
3. un commit de contrôle passé par les hooks installés, avec son reçu
   (`provenance show`).

Elle lève la prise et révoque ses liaisons de test. `retour` revient au venv
précédent sans réseau.

Chaque session du bureau commence ensuite par
`ameesh mail bind bureau --session <id> --harness <harnais> --pid <PID du harnais>`.
Avant le premier geste sur un hôte ou un geste à dépense, elle lance
`ameesh work claim <lot> --session <id>`.

## Retrait

1. `REVOKE ALL` puis `DROP OWNED BY bureau_goal; DROP ROLE bureau_goal` (la
   ligne de `limited_logins` peut rester : elle n'a d'effet que pour ce rôle).
2. Retrait du pair : `systemctl disable --now ameesh-bg-peer.service` puis
   suppression de l'unité (mesh manaty), sinon retrait du bloc dans
   `/etc/wireguard/wg0.conf` du hub et `wg set wg0 peer <clé> remove`.
3. Retrait de la ligne pg_hba.

Ce sont trois gestes distincts (§10).
