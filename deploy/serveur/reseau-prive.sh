#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Réseau privé des nœuds de base (EP8.2, docs/design/etudes/replique-base-mesh.md
# §1 et §8 ; mode d'emploi : deploy/serveur/RESEAU-PRIVE.md). En root, sur
# chaque nœud, après le rattachement de sa carte (terraform/reseau-prive) :
#
#   reseau-prive.sh interface <adresse/préfixe> [--mac <mac>]
#       adresse fixe sur la carte privée, sans passerelle ni DNS ; persistante
#       (systemd-networkd, sinon ifupdown). Rejouable.
#   reseau-prive.sh postgres <adresse> <sous-réseau> [--redemarrer]
#       nœud qui porte Postgres : écoute aussi sur l'adresse privée, pg_hba
#       hostssl + scram-sha-256 sur le sous-réseau, attente de l'adresse au
#       démarrage. pg_hba est rechargé ; listen_addresses demande un
#       redémarrage, fait seulement avec --redemarrer (créneau, étude décision 8).
#   reseau-prive.sh verifier <sous-réseau> <pair> [<pair>...] [--hub-arrete]
#       pairs joignables sur le réseau privé (Postgres s'il écoute), aucune
#       base sur une adresse publique ; --hub-arrete exige en plus que le hub
#       WireGuard (10.77.0.1) soit injoignable : l'essai de l'acceptation.
#
# RACINE préfixe les chemins de /etc (tests) ; rien d'autre ne change.
set -euo pipefail

RACINE=${RACINE:-}
METADONNEES=${METADONNEES:-http://169.254.42.42/conf?format=json}
PG_VERSION=${PG_VERSION:-17}
PG_CONF=$RACINE/etc/postgresql/$PG_VERSION/main
MARQUE='# ameesh-reseau-prive (EP8.2)'
HUB=${HUB:-10.77.0.1}

log() { printf '[reseau-prive] %s\n' "$*" >&2; }
meurt() { log "$*"; exit 2; }

ipv4_valide() {
  local a=$1 o
  [[ $a =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for o in "${BASH_REMATCH[@]:1}"; do ((10#$o <= 255)) || return 1; done
}

# Adresse privée RFC 1918 seulement, hors WireGuard (10.77.0.0/24) : une erreur
# de saisie ne doit pas ouvrir pg_hba à une plage publique ou à tous les appareils.
prive() {
  local a=${1%/*}
  ipv4_valide "$a" || return 1
  [[ $a == 10.77.0.* ]] && return 1
  [[ $a == 10.* || $a == 192.168.* ]] && return 0
  [[ $a =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]]
}

# Réseau canonique (/16 à /29) entièrement RFC 1918 et sans recouvrement de
# WireGuard : tester la seule adresse de base laissait passer 10.77.1.0/16.
# --hote : adresse/préfixe d'une interface, jugée sur le réseau qu'elle porte.
sous_reseau_valide() {
  python3 -I -c '
import ipaddress, sys
try:
    if sys.argv[1] == "--hote":
        n = ipaddress.IPv4Interface(sys.argv[2]).network
    else:
        n = ipaddress.IPv4Network(sys.argv[1])
except ValueError:
    sys.exit(1)
rfc1918 = [ipaddress.IPv4Network(c) for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
ok = (16 <= n.prefixlen <= 29 and any(n.subnet_of(r) for r in rfc1918)
      and not n.overlaps(ipaddress.IPv4Network("10.77.0.0/24")))
sys.exit(0 if ok else 1)' "$@"
}

# MAC de l'unique carte privée annoncée par les métadonnées Scaleway.
mac_privee() {
  curl -fsS --max-time 5 "$METADONNEES" | python3 -I -c '
import json, sys
nics = json.load(sys.stdin).get("private_nics") or []
if len(nics) != 1:
    sys.exit(f"{len(nics)} carte(s) privée(s) : passer --mac")
print(nics[0]["mac_address"].lower())'
}

interface_de() {
  ip -o link | awk -v mac="$1" 'tolower($0) ~ "link/ether " mac {sub(":$", "", $2); sub("@.*", "", $2); print $2; exit}'
}

cmd_interface() {
  local cidr=${1:-} mac=''
  shift || true
  while (($#)); do
    case $1 in --mac) mac=${2:?--mac sans valeur}; shift 2 ;; *) meurt "argument inconnu : $1" ;; esac
  done
  [[ $cidr =~ ^[0-9.]+/[0-9]{1,2}$ ]] && prive "$cidr" && sous_reseau_valide --hote "$cidr" \
    || meurt "adresse/préfixe privé attendu (/16 à /29) : '$cidr'"
  [[ -n $mac ]] || mac=$(mac_privee) || meurt "MAC de la carte privée introuvable"
  mac=${mac,,}
  [[ $mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] || meurt "MAC invalide : $mac"
  local nom
  nom=$(interface_de "$mac")
  [[ -n $nom ]] || meurt "aucune interface pour $mac : carte non rattachée ?"
  if systemctl -q is-active systemd-networkd; then
    install -d "$RACINE/etc/systemd/network"
    cat > "$RACINE/etc/systemd/network/20-ameesh-prive.network" <<CONF
$MARQUE
# Carte privée : adresse fixe (réservée dans l'IPAM), ni passerelle, ni DNS.
[Match]
MACAddress=$mac

[Link]
RequiredForOnline=no

[Network]
Address=$cidr
DHCP=no
LinkLocalAddressing=no
IPv6AcceptRA=no
CONF
    networkctl reload
    networkctl reconfigure "$nom"
  else
    install -d "$RACINE/etc/network/interfaces.d"
    cat > "$RACINE/etc/network/interfaces.d/ameesh-prive" <<CONF
$MARQUE
auto $nom
iface $nom inet static
    address $cidr
CONF
    ifup "$nom"
  fi
  local a
  for _ in $(seq 10); do
    a=$(ip -4 -o addr show dev "$nom" | awk '{print $4}' || true)
    [[ " $a " == *" $cidr "* ]] && { log "$nom ($mac) : $cidr"; return 0; }
    sleep 1
  done
  meurt "$nom n'a pas pris $cidr"
}

cmd_postgres() {
  local adresse=${1:-} sous_reseau=${2:-} redemarrer=${3:-}
  prive "$adresse" && [[ $adresse != */* ]] || meurt "adresse privée attendue : '$adresse'"
  sous_reseau_valide "$sous_reseau" || meurt "sous-réseau privé attendu (/16 à /29) : '$sous_reseau'"
  [[ -z $redemarrer || $redemarrer == --redemarrer ]] || meurt "argument inconnu : $redemarrer"
  [[ -d $PG_CONF/conf.d ]] || meurt "pas de cluster Postgres $PG_VERSION ici ($PG_CONF)"
  # L'adresse WireGuard reste écoutée : le poste et les appareils y passent
  # tant que la base primaire est ce nœud (chemin du poste, RESEAU-PRIVE.md).
  local ecoute="localhost" wg
  wg=$(ip -4 -o addr show dev wg0 2>/dev/null | awk '{sub("/.*", "", $4); print $4; exit}' || true)
  [[ -n $wg ]] && ecoute+=",$wg"
  ecoute+=",$adresse"
  local conf=$PG_CONF/conf.d/reseau-prive.conf avant=''
  [[ -f $conf ]] && avant=$(<"$conf")
  # conf.d est lu dans l'ordre alphabétique : ce fichier passe après ameesh.conf.
  local apres="$MARQUE
listen_addresses = '$ecoute'"
  if [[ $avant != "$apres" ]]; then
    printf '%s\n' "$apres" | install -m 0644 -o postgres -g postgres /dev/stdin "$conf"
  fi
  local hba=$PG_CONF/pg_hba.conf
  local ligne="hostssl ameesh  all  $sous_reseau  scram-sha-256"
  if ! grep -qxF "$ligne" "$hba"; then
    sed -i "/^$MARQUE\$/,+1d" "$hba"
    printf '%s\n%s\n' "$MARQUE" "$ligne" >> "$hba"
  fi
  install -d "$RACINE/etc/systemd/system/postgresql@.service.d"
  # Sans son adresse, Postgres démarre quand même (il ignore une adresse
  # absente) mais n'écoute plus le réseau privé : attendre la carte, au plus 30 s.
  cat > "$RACINE/etc/systemd/system/postgresql@.service.d/reseau-prive.conf" <<CONF
$MARQUE
[Unit]
After=systemd-networkd.service networking.service

[Service]
ExecStartPre=-/usr/bin/timeout 30 /bin/sh -c 'until ip -4 -o addr show to $adresse/32 | grep -q .; do sleep 1; done'
CONF
  systemctl daemon-reload
  runuser -u postgres -- psql -qAt -c 'SELECT pg_reload_conf()' >/dev/null
  local actuel
  actuel=$(runuser -u postgres -- psql -qAt -c 'SHOW listen_addresses')
  if [[ $actuel == "$ecoute" ]]; then
    log "Postgres écoute déjà $ecoute ; pg_hba rechargé"
  elif [[ -n $redemarrer ]]; then
    systemctl restart "postgresql@$PG_VERSION-main"
    for _ in $(seq 30); do pg_isready -q -h localhost && break; sleep 1; done
    pg_isready -h localhost >/dev/null || meurt "Postgres ne répond plus après le redémarrage"
    log "Postgres redémarré : écoute $ecoute"
  else
    log "pg_hba rechargé ; écoute '$actuel' → '$ecoute' au prochain redémarrage (--redemarrer, dans le créneau)"
  fi
}

# Adresse dans le sous-réseau ? (comparaison des bits du préfixe)
dans() {
  local a=$1 res=${2%/*} p=${2#*/} x y
  IFS=. read -r a1 a2 a3 a4 <<<"$a"
  IFS=. read -r r1 r2 r3 r4 <<<"$res"
  x=$(( (a1 << 24) | (a2 << 16) | (a3 << 8) | a4 ))
  y=$(( (r1 << 24) | (r2 << 16) | (r3 << 8) | r4 ))
  (( (x ^ y) >> (32 - p) == 0 ))
}

cmd_verifier() {
  local sous_reseau=${1:-} hub_arrete='' pairs=()
  shift || true
  while (($#)); do
    case $1 in --hub-arrete) hub_arrete=1 ;; *) pairs+=("$1") ;; esac
    shift
  done
  sous_reseau_valide "$sous_reseau" || meurt "sous-réseau privé attendu : '$sous_reseau'"
  ((${#pairs[@]})) || meurt "au moins un pair"
  local echecs=0 pair
  # Aucune base publique : Postgres n'écoute que sur la boucle locale,
  # WireGuard (10.77.0.0/24) ou le sous-réseau privé.
  local adr
  while read -r adr; do
    adr=${adr%:*}; adr=${adr#[}; adr=${adr%]}
    case $adr in
      127.* | ::1 | 10.77.0.*) ;;
      *) if ipv4_valide "$adr" && dans "$adr" "$sous_reseau"; then :; else
           log "ÉCHEC base exposée : Postgres écoute sur $adr"; echecs=$((echecs + 1)); fi ;;
    esac
  done < <(ss -ltnH 'sport = :5432' | awk '{print $4}')
  if ping -c 1 -W 2 -q "$HUB" >/dev/null 2>&1; then
    log "hub WireGuard $HUB joignable"
    [[ -z $hub_arrete ]] || { log "ÉCHEC essai hub arrêté : $HUB répond encore"; echecs=$((echecs + 1)); }
  else
    log "hub WireGuard $HUB injoignable"
  fi
  for pair in "${pairs[@]}"; do
    { ipv4_valide "$pair" && dans "$pair" "$sous_reseau"; } || meurt "pair hors du sous-réseau : $pair"
    if ! ping -c 2 -W 2 -q "$pair" >/dev/null 2>&1; then
      log "ÉCHEC $pair injoignable"; echecs=$((echecs + 1)); continue
    fi
    if timeout 3 bash -c "exec 3<>/dev/tcp/$pair/5432" 2>/dev/null; then
      if pg_isready -q -h "$pair" -p 5432 -t 3; then log "ok $pair : Postgres prêt"
      else log "ÉCHEC $pair : port 5432 ouvert, Postgres pas prêt"; echecs=$((echecs + 1)); fi
    else
      log "ok $pair joignable (pas de Postgres)"
    fi
  done
  ((echecs == 0)) || meurt "$echecs échec(s)"
  log "réseau privé conforme"
}

case ${1:-} in
  interface) shift; cmd_interface "$@" ;;
  postgres) shift; cmd_postgres "$@" ;;
  verifier) shift; cmd_verifier "$@" ;;
  *) sed -n '4,21p' "$0" >&2; exit 2 ;;
esac
