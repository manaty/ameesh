# SPDX-License-Identifier: AGPL-3.0-only
#
# Réseau privé des nœuds de base d'un mesh (EP8.2, étude
# docs/design/etudes/replique-base-mesh.md §1). Racine Terraform distincte de
# celle de la VM du mesh : elle rattache des serveurs EXISTANTS (créés par
# ../ ou à la main) à un réseau privé Scaleway, avec une adresse réservée
# (IPAM) par serveur. Elle ne crée, ne modifie ni ne détruit aucun serveur.
#
# Les groupes de sécurité Scaleway ne filtrent que l'interface publique : le
# réseau privé est fermé par construction (projet seul) et Postgres le filtre
# par pg_hba (deploy/serveur/reseau-prive.sh). Aucun port public n'est ouvert.

terraform {
  required_version = ">= 1.5"
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.40"
    }
  }
}

provider "scaleway" {
  # Profil nommé de l'organisation, jamais un profil par défaut (0033 §1).
  profile    = var.scw_profile
  zone       = var.zone
  region     = var.region
  project_id = var.project_id
}

data "scaleway_account_project" "mesh" {
  project_id = var.project_id
}

# Garde-fou : chaque serveur rattaché est dans le projet et la zone annoncés.
# Un réseau privé ne traverse ni projet ni région : sinon, repli WireGuard
# (étude §1, « Fallback »).
data "scaleway_instance_server" "noeud" {
  for_each  = var.noeuds
  server_id = each.value.server_id
  zone      = var.zone
}

resource "scaleway_vpc_private_network" "base" {
  name = "${var.organisation}-base"
  tags = ["ameesh", "ep8", var.organisation]

  ipv4_subnet {
    subnet = var.sous_reseau
  }

  lifecycle {
    precondition {
      condition     = data.scaleway_account_project.mesh.organization_id == var.organization_id
      error_message = "Le projet ${var.project_id} n'appartient pas à l'organisation ${var.organization_id} : mauvais profil Scaleway ?"
    }
    precondition {
      condition     = alltrue([for n in data.scaleway_instance_server.noeud : n.project_id == var.project_id])
      error_message = "Un serveur n'est pas dans le projet ${var.project_id} : réseau privé impossible, repli WireGuard."
    }
  }
}

# Adresse fixe par nœud : la base, pg_hba et les DSN la citent ; un bail
# DHCP qui change ne doit pas la déplacer.
resource "scaleway_ipam_ip" "noeud" {
  for_each = var.noeuds
  address  = each.value.adresse
  tags     = ["ameesh", "ep8", each.key]

  source {
    private_network_id = scaleway_vpc_private_network.base.id
  }

  lifecycle {
    precondition {
      condition     = cidrhost("${each.value.adresse}/${split("/", var.sous_reseau)[1]}", 0) == cidrhost(var.sous_reseau, 0)
      error_message = "L'adresse ${each.value.adresse} de ${each.key} n'est pas dans ${var.sous_reseau}."
    }
  }
}

# Rattacher une carte à une VM en marche n'arrête pas la VM ; l'interface
# apparaît sans adresse tant que reseau-prive.sh ne l'a pas configurée.
resource "scaleway_instance_private_nic" "noeud" {
  for_each           = var.noeuds
  server_id          = each.value.server_id
  private_network_id = scaleway_vpc_private_network.base.id
  ipam_ip_ids        = [scaleway_ipam_ip.noeud[each.key].id]
  tags               = ["ameesh", "ep8", each.key]
}
