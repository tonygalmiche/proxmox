# migrer-vm-opennebula-vers-proxmox.sh

Script de migration d'une VM depuis un serveur **OpenNebula** vers un serveur **Proxmox**.

Le script est lancé **depuis le serveur Proxmox**, qui dispose d'un accès SSH complet
(avec clé) vers le serveur OpenNebula. Toutes les commandes `onevm` / `oneimage` sont donc
exécutées à distance via `ssh`, et les commandes `qm` / `pvesm` sont exécutées localement.

Pré-requis : la VM doit déjà exister sur Proxmox, sous le **même nom** que sur OpenNebula,
avec le même nombre de disques (créés à la bonne taille).

## Usage

```bash
./migrer-vm-opennebula-vers-proxmox.sh <nom-vm>
```

Exemple :

```bash
./migrer-vm-opennebula-vers-proxmox.sh vm-dokuwiki-bullseye
```

## Configuration

Les paramètres sont centralisés dans [config.sh](config.sh) (sourcé automatiquement par le
script, doit se trouver dans le même dossier) :

- `OPENNEBULA_HOST` : alias/host SSH (dans `~/.ssh/config`) du serveur OpenNebula (`info-pra` par défaut).
- `OPENNEBULA_DATASTORES` : identifiant du datastore OpenNebula contenant les images disques
  (utilisé en étape 5, chemin `/var/lib/one/datastores/$OPENNEBULA_DATASTORES/<image>`).

## Étapes du script

### 1. Vérification de l'état de la VM sur OpenNebula

OpenNebula suffixe le nom des VM avec leur ID (ex: `vm-dokuwiki-bullseye-203`). Le script
exécute donc `onevm list -x` via SSH et recherche la VM dont le nom est exactement
`<nom-vm>` ou correspond au motif `<nom-vm>-<id>`. Si aucune VM ne correspond, ou si
plusieurs VM correspondent (cas ambigu), le script s'arrête en erreur.

Une fois la VM identifiée par son ID, le script exécute `onevm show <id> -x` et vérifie
que l'état (`STATE`) est bien `STOPPED`, `POWEROFF` ou `UNDEPLOYED`. Si la VM est dans un
autre état (`ACTIVE`, `SUSPENDED`, etc.), le script affiche une erreur et s'arrête
immédiatement (aucune autre étape n'est exécutée).

### 2. Récupération des disques sur OpenNebula

Toujours via `onevm show <id> -x`, le script récupère la liste des disques de la VM :
`DISK_ID`, nom de l'image (`IMAGE`), taille en Mo (`SIZE`) et device cible (`TARGET`).

### 3. Récupération des disques sur Proxmox

Le script retrouve l'ID de la VM Proxmox correspondant au nom passé en paramètre
(via `qm list`), puis liste ses disques avec `qm config <vmid>` (lignes
`scsi*`, `virtio*`, `ide*`, `sata*`) et en extrait la taille (paramètre `size=`).

### 4. Vérification de la concordance des disques

Le script compare, disque par disque (par ordre d'index), la taille du disque OpenNebula
et celle du disque Proxmox correspondant. Un tableau récapitulatif est affiché avec le
statut `OK` ou `MISMATCH` pour chaque disque, ainsi qu'un contrôle du nombre total de
disques. Si une incohérence est détectée (nombre de disques différent ou taille différente),
le script s'arrête en erreur sans rien modifier.

> Les étapes 1 à 4 sont **uniquement des vérifications en lecture seule** : aucune donnée
> n'est copiée ni modifiée sur OpenNebula ou sur Proxmox.

### 5. Copie des données des disques vers Proxmox

⚠️ Pas d'espace disque suffisant sur Proxmox pour copier les images brutes complètes
(problème bloquant initial). À la place, les **données** sont synchronisées au niveau
fichier (montage + `rsync`), sans jamais stocker l'image complète sur Proxmox : voir
[synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md).
Cette approche permet aussi de relancer la synchronisation plusieurs fois (mode par
défaut, rapide) avant la bascule finale.

Pré-requis : les disques Proxmox vides doivent déjà exister, créés par
[creer-vm-proxmox.sh](creer-vm-proxmox.md).

## Scripts du workflow complet

1. [creer-vm-proxmox.sh](creer-vm-proxmox.md) : crée la VM Proxmox (CPU/RAM/disques vides)
   si elle n'existe pas déjà.
2. `migrer-vm-opennebula-vers-proxmox.sh` (ce script) : vérifie que tout concorde
   (étapes 1 à 4 ci-dessus).
3. [synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md) :
   copie les données des disques (étape 5) — premier passage avec `--init`, puis
   resynchronisations en mode par défaut autant que nécessaire.

## État d'avancement

- [x] Étape 1 : vérification VM arrêtée sur OpenNebula
- [x] Étape 2 : liste des disques OpenNebula
- [x] Étape 3 : liste des disques Proxmox
- [x] Étape 4 : vérification de concordance des tailles
- [x] Étape 5 : copie des données des disques (synchroniser-vm-opennebula-vers-proxmox.sh)
