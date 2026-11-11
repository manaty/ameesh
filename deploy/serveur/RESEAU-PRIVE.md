# Réseau privé des nœuds de base (EP8.2)

Étude : [`docs/design/etudes/replique-base-mesh.md`](../../docs/design/etudes/replique-base-mesh.md)
§1 et §8 ; décision owner #207987 option a. Lot #6859 (manaty/ameesh#5216).

La base et le hub WireGuard ne doivent plus partager une panne : les nœuds de
base se joignent par un **réseau privé Scaleway**, sans passer par le hub.
Le hub reste le serveur WireGuard du poste et des autres appareils.

```
  pc-smichea 10.77.0.2 ──WireGuard──► manaty-mesh-1 (hub 10.77.0.1)
                                         │ 10.78.0.10   Postgres (primaire)
                    réseau privé ────────┼──────────────────────────────
                    10.78.0.0/24         │
      ameesh-agents-1 10.78.0.11   ameesh-agents-2 10.78.0.12   ameesh-agents-3 10.78.0.13
```

Conditions vérifiées par le bureau le 2026-11-11 (lecture seule, API
Scaleway, note du lot) : les quatre VM sont dans le **même projet**
(`ab1a8716-…`) et la **même zone** (`fr-par-1`), sans carte privée ; aucun
réseau privé existant n'est réutilisable. Le repli WireGuard partiel (étude
§1, « Fallback ») n'est donc pas nécessaire.

| Fichier | Rôle |
|---|---|
| `terraform/reseau-prive/` | crée le réseau privé, réserve une adresse par nœud (IPAM) et rattache une carte privée aux VM **existantes** ; ne crée ni ne modifie aucun serveur ; refuse un serveur hors du projet |
| `reseau-prive.sh interface` | adresse fixe sur la carte privée, sans passerelle ni DNS, persistante (systemd-networkd, sinon ifupdown) |
| `reseau-prive.sh postgres` | Postgres écoute aussi l'adresse privée ; `pg_hba` : `hostssl` + `scram-sha-256` sur le sous-réseau seul ; attente de la carte au démarrage |
| `reseau-prive.sh verifier` | pairs joignables, Postgres prêt là où il écoute, aucune base sur une adresse publique ; `--hub-arrete` exige un hub injoignable |

## Plan d'adresses

| Hôte | Adresse privée | Rôle aujourd'hui | Rôle EP8.3+ |
|---|---|---|---|
| `manaty-mesh-1` (`ameesh-mesh-manaty`) | `10.78.0.10` | primaire, hub | nœud `nofailover`/`nosync` |
| `ameesh-agents-1` | `10.78.0.11` | client | candidat primaire |
| `ameesh-agents-2` | `10.78.0.12` | client | candidat primaire |
| `ameesh-agents-3` | `10.78.0.13` | client | client |

`10.78.0.0/24` ne recoupe ni WireGuard (`10.77.0.0/24`) ni Docker
(`172.17.0.0/16`). Les premières adresses d'un sous-réseau Scaleway sont
réservées : les nœuds commencent à `.10`.

## Sécurité

- **Aucun port public** : les groupes de sécurité Scaleway ne filtrent que
  l'interface publique et ne changent pas ; 5432 n'y est jamais ouvert.
- Le réseau privé n'existe que dans le projet ; seules les quatre VM y sont
  rattachées. Postgres n'y admet que `hostssl … scram-sha-256` sur
  `10.78.0.0/24` ; le script refuse un sous-réseau public, plus large que
  /16 ou recouvrant WireGuard.
- TLS reste `sslmode=require` (certificat snakeoil) : le réseau privé et le
  mot de passe authentifient ; une CA du mesh (`verify-full`) est une suite
  (étude §8).
- Les règles `replication` de `pg_hba` et le rôle `replicator` viennent avec
  EP8.3 ; etcd et l'API Patroni écouteront l'adresse privée seule (EP8.4).

## Mise en place

Classe sensible (infrastructure, droits) : chaque étape est un geste du
**bureau**, après relecture du lot. Staging seulement (§14).

1. **Réseau et cartes** (aucune coupure : la carte apparaît sans adresse,
   l'interface publique et WireGuard ne bougent pas) :

   ```bash
   cd deploy/serveur/terraform/reseau-prive
   cat > base.auto.tfvars <<'EOF'
   scw_profile     = "<profil nommé>"
   organization_id = "<organisation Scaleway>"
   project_id      = "ab1a8716-3eaf-4671-906f-d319fde5d484"
   organisation    = "manaty"
   noeuds = {
     "manaty-mesh-1"   = { server_id = "<id ameesh-mesh-manaty>", adresse = "10.78.0.10" }
     "ameesh-agents-1" = { server_id = "<id>", adresse = "10.78.0.11" }
     "ameesh-agents-2" = { server_id = "<id>", adresse = "10.78.0.12" }
     "ameesh-agents-3" = { server_id = "<id>", adresse = "10.78.0.13" }
   }
   EOF
   terraform init && terraform plan && terraform apply
   terraform output mac
   ```

   Le plan ne doit contenir que des créations (`private_network`, quatre
   `ipam_ip`, quatre `private_nic`). L'état reste hors du dépôt.
   Un réseau privé et ses cartes sont gratuits chez Scaleway : à reconfirmer
   au geste ; un coût nouveau irait à l'owner d'abord (§10).

2. **Adresse de chaque carte**, en root sur chaque VM (MAC lue dans les
   métadonnées, ou `--mac` depuis `terraform output mac`) :

   ```bash
   /opt/ameesh/src/deploy/serveur/reseau-prive.sh interface 10.78.0.1N/24
   ```

   Sur les VM d'agents (Ubuntu 24.04, netplan + networkd), le fichier
   `/etc/systemd/network/20-ameesh-prive.network` ne touche que la carte
   privée (correspondance par MAC) : `networkctl reload` ne relance pas
   `ens2`.

3. **Postgres sur `manaty-mesh-1`** :

   ```bash
   /opt/ameesh/src/deploy/serveur/reseau-prive.sh postgres 10.78.0.10 10.78.0.0/24
   ```

   `pg_hba` est rechargé tout de suite ; l'écoute de l'adresse privée
   demande un redémarrage (30 s environ ; les clients réessaient 60 s,
   L72). Dans un créneau convenu avec le pilote (décision 8 de l'étude, le
   même que le redémarrage d'EP8.1 s'il est proche) :

   ```bash
   /opt/ameesh/src/deploy/serveur/reseau-prive.sh postgres 10.78.0.10 10.78.0.0/24 --redemarrer
   ```

4. **Clients sur l'adresse privée** : sur chaque VM d'agents, sous
   `ameesh`, l'amont de PgBouncer passe de `10.77.0.1` à `10.78.0.10` ;
   les sessions en cours finissent sur l'ancien chemin, les nouvelles
   prennent le réseau privé :

   ```bash
   sed -i 's/host=10\.77\.0\.1 /host=10.78.0.10 /' ~/.config/pgbouncer/pgbouncer.ini
   systemctl --user kill -s HUP ameesh-pgbouncer.service
   grep -q '^10.78.0.10:' ~/.pgpass || sed -n 's/^10\.77\.0\.1:/10.78.0.10:/p' ~/.pgpass >> ~/.pgpass
   ameesh doctor
   ```

   Contrôle sur `manaty-mesh-1` : `SELECT client_addr, count(*) FROM
   pg_stat_activity GROUP BY 1` montre `10.78.0.11-13` et plus `10.77.0.3-5`
   (après la rotation des sessions).

## Essai hub arrêté (acceptation)

Court (moins de 5 min) ; le poste perd la base pendant l'essai (voir plus
bas) : prévenir le pilote, PC sans tour en cours si possible.

1. Avant : sur chaque VM, `reseau-prive.sh verifier 10.78.0.0/24 <les trois
   autres>` est vert.
2. Sur `manaty-mesh-1` : `systemctl stop wg-quick@wg0`.
3. Sur `ameesh-agents-1`, `-2`, `-3` :

   ```bash
   reseau-prive.sh verifier 10.78.0.0/24 10.78.0.10 <les deux autres VM d'agents> --hub-arrete
   runuser -l ameesh -c 'ameesh doctor'
   ```

   et sur `manaty-mesh-1` : `reseau-prive.sh verifier 10.78.0.0/24
   10.78.0.11 10.78.0.12 10.78.0.13 --hub-arrete`.
4. `systemctl start wg-quick@wg0` ; le poste retrouve la base (Postgres
   garde sa socket sur `10.77.0.1` pendant l'arrêt de `wg0` : vérifier
   `ss -ltn 'sport = :5432'` et une connexion du poste).

Joindre au lot les sorties de l'étape 3 (sans DSN ni mot de passe : le
script n'en affiche pas). L'acceptation : chaque nœud joint les autres hub
arrêté ; `verifier` n'a relevé aucune base exposée.

## Chemin du poste

Le poste (`pc-smichea`, `10.77.0.2`) et `ameesh-approve` n'ont pas de carte
privée : ils passent par le hub.

- **Aujourd'hui (primaire = `manaty-mesh-1`)** : rien ne change. Le poste
  garde `postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require` et
  `AllowedIPs = 10.77.0.1/32`. Hub arrêté, le poste et `ameesh-approve`
  perdent la base jusqu'au retour du hub ; les VM continuent. C'est le choix
  de l'étude (§1) : le poste n'est ni votant ni sur le chemin des VM.
- **Quand le primaire peut quitter le hub (EP8.4, bascule EP8.6)** : le hub
  relaie le seul poste vers le réseau privé, en NAT (la source vue des nœuds
  est `10.78.0.10`, déjà admise par `pg_hba`) :

  ```bash
  # manaty-mesh-1, root ; <prive> = interface privée (ip -br addr | grep 10.78.0.10)
  sysctl -w net.ipv4.ip_forward=1   # puis persister dans /etc/sysctl.d/
  nft add table ip ameesh_pc
  nft add chain ip ameesh_pc fwd '{ type filter hook forward priority 0; policy accept; }'
  nft add rule ip ameesh_pc fwd iifname wg0 ip saddr != 10.77.0.2 ip daddr 10.78.0.0/24 drop
  nft add chain ip ameesh_pc nat '{ type nat hook postrouting priority 100; }'
  nft add rule ip ameesh_pc nat ip saddr 10.77.0.2 ip daddr 10.78.0.0/24 oifname <prive> masquerade
  ```

  Sur le poste : `AllowedIPs = 10.77.0.1/32, 10.78.0.0/24` dans
  `/etc/wireguard/ameesh.conf`, puis le DSN multi-hôte d'EP8.4/EP8.5
  (`host=10.78.0.11,10.78.0.12,10.78.0.10 target_session_attrs=read-write`)
  pour le poste et `ameesh-approve`. Ce relais n'est pas posé par EP8.2 : il
  n'a pas d'usage tant que le primaire est le hub, et chaque règle ouverte
  sans usage est une surface de plus.
- **Hub perdu, primaire ailleurs** : le poste attend le hub. Un pair
  WireGuard direct poste → VM d'agents est possible plus tard (clé, port UDP
  ouvert dans le groupe de sécurité de cette VM) ; ce n'est pas un besoin
  d'EP8 (étude §1).

## Retour arrière

- Clients : remettre `host=10.77.0.1` dans `pgbouncer.ini`, `HUP`.
- Postgres : supprimer `conf.d/reseau-prive.conf`, le bloc
  `# ameesh-reseau-prive (EP8.2)` de `pg_hba.conf` et le drop-in
  `postgresql@.service.d/reseau-prive.conf` ; `pg_reload_conf()` (l'écoute
  privée reste jusqu'au prochain redémarrage, sans effet une fois les
  clients revenus).
- Cartes : supprimer `20-ameesh-prive.network` (ou
  `interfaces.d/ameesh-prive`), puis `terraform destroy` dans
  `terraform/reseau-prive` : il ne retire que le réseau, les réservations
  et les cartes.
