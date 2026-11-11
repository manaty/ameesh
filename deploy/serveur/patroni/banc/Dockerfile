# Image jetable du banc local de la grappe (lot #6902, EP8.6b).
#
# PostgreSQL 17 vient de l'image officielle ; Patroni et etcd sont ajoutés
# dans la même image parce que le banc reproduit la colocation réelle : sur
# chaque nœud, etcd et Patroni tournent à côté du PostgreSQL qu'ils gèrent.
# Aucun secret, aucun certificat et aucune adresse du mesh n'entrent ici.
FROM postgres:17-bookworm

ARG ETCD_VERSION=3.5.17
ARG ETCD_SHA256=eff6ac621d41711085d0f38fab17d8fa3705f6326c3ff11301a1f5a71fc94edd

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        iptables \
        procps \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Patroni 4.x et son pilote psycopg, comme /opt/patroni/venv du runbook #6870.
RUN python3 -m venv /opt/patroni/venv \
    && /opt/patroni/venv/bin/pip install --no-cache-dir \
        'patroni[etcd]>=4.0,<5' \
        'psycopg[binary]>=3.2'

# etcd 3.5.17, version et empreinte validées par #6870 (le paquet Debian est
# en 3.4). L'empreinte est vérifiée avant installation : une archive
# substituée fait échouer la construction.
RUN set -eux; \
    curl -fsSL -o /tmp/etcd.tgz \
        "https://github.com/etcd-io/etcd/releases/download/v${ETCD_VERSION}/etcd-v${ETCD_VERSION}-linux-amd64.tar.gz"; \
    echo "${ETCD_SHA256}  /tmp/etcd.tgz" | sha256sum -c -; \
    tar -xzf /tmp/etcd.tgz -C /tmp; \
    install -m 0755 "/tmp/etcd-v${ETCD_VERSION}-linux-amd64/etcd" /usr/local/bin/etcd; \
    install -m 0755 "/tmp/etcd-v${ETCD_VERSION}-linux-amd64/etcdctl" /usr/local/bin/etcdctl; \
    rm -rf /tmp/etcd.tgz "/tmp/etcd-v${ETCD_VERSION}-linux-amd64"

COPY entree-noeud.sh /usr/local/bin/entree-noeud.sh
COPY client-ecriture.sh /usr/local/bin/client-ecriture.sh
RUN chmod 0755 /usr/local/bin/entree-noeud.sh /usr/local/bin/client-ecriture.sh

# Les configurations, le matériel TLS et les données sont montés par banc.sh ;
# l'image ne contient ni nœud ni adresse.
ENTRYPOINT ["/usr/local/bin/entree-noeud.sh"]
