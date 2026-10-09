# SPDX-License-Identifier: AGPL-3.0-only

variable "project_id" {
  description = "Projet de l'hébergeur où créer la VM : celui de l'ORGANISATION du mesh (0033 §1), jamais celui d'une autre."
  type        = string
}

variable "organisation" {
  description = "Identifiant court de l'organisation (étiquette, nom du mesh)."
  type        = string
}

variable "name" {
  description = "Nom de la VM."
  type        = string
  default     = "ameesh-mesh"
}

variable "instance_type" {
  description = "Gabarit de la VM. Les agents lancent des tests (conteneurs, suites JS) : prévoir de la mémoire."
  type        = string
  default     = "PRO2-S"
}

variable "disk_gb" {
  description = "Taille du volume racine (Go) : sessions, dépôts de travail, images de conteneurs."
  type        = number
  default     = 200
}

variable "ssh_cidrs" {
  description = "Plages autorisées en SSH (administration). Vide = SSH fermé, accès par WireGuard seulement."
  type        = list(string)
  default     = []
}

variable "wireguard_port" {
  description = "Port UDP de WireGuard."
  type        = number
  default     = 51820
}

variable "ameesh_ref" {
  description = "Version d'ameesh installée (étiquette ou commit de manaty/ameesh)."
  type        = string
  default     = "main"
}

variable "zone" {
  type    = string
  default = "fr-par-1"
}

variable "region" {
  type    = string
  default = "fr-par"
}
