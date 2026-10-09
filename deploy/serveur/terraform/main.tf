# SPDX-License-Identifier: AGPL-3.0-only
#
# Profil serveur VM d'un mesh ameesh (lot L55, décisions 0033 §2, étude v2 A1–A2).
# Une VM par organisation, chez l'hébergeur de l'organisation. Aucun port
# public hormis SSH (restreint) et WireGuard : Postgres n'écoute que sur le
# réseau privé WireGuard des appareils du mesh.

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
  # Profil nommé de l'organisation (~/.config/scw/config.yaml), jamais un
  # profil par défaut : aucune ambiguïté sur l'organisation visée (0033 §1).
  profile    = var.scw_profile
  zone       = var.zone
  region     = var.region
  project_id = var.project_id
}

# Garde-fou : le projet doit appartenir à l'organisation annoncée.
data "scaleway_account_project" "mesh" {
  project_id = var.project_id
}

resource "scaleway_instance_ip" "mesh" {
  lifecycle {
    precondition {
      condition     = data.scaleway_account_project.mesh.organization_id == var.organization_id
      error_message = "Le projet ${var.project_id} n'appartient pas à l'organisation ${var.organization_id} : mauvais profil Scaleway ?"
    }
  }
}

resource "scaleway_instance_security_group" "mesh" {
  name                    = "${var.name}-sg"
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"

  dynamic "inbound_rule" {
    for_each = var.ssh_cidrs
    content {
      action   = "accept"
      protocol = "TCP"
      port     = 22
      ip_range = inbound_rule.value
    }
  }

  inbound_rule {
    action   = "accept"
    protocol = "UDP"
    port     = var.wireguard_port
    ip_range = "0.0.0.0/0"
  }
}

resource "scaleway_instance_server" "mesh" {
  name              = var.name
  type              = var.instance_type
  image             = "debian_bookworm"
  ip_id             = scaleway_instance_ip.mesh.id
  security_group_id = scaleway_instance_security_group.mesh.id
  tags              = ["ameesh", "mesh", var.organisation]

  root_volume {
    size_in_gb  = var.disk_gb
    volume_type = "sbs_volume"
  }

  user_data = {
    cloud-init = templatefile("${path.module}/../cloud-init.yaml", {
      organisation   = var.organisation
      wireguard_port = var.wireguard_port
      ameesh_ref     = var.ameesh_ref
    })
  }
}
