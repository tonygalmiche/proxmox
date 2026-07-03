# migrer-vm-opennebula-vers-proxmox.py

Script de migration d'une VM d'OpenNebula vers Proxmox via rsync (pas de copie brute de l'image disque).

## Fonctionnement

1. Connecte l'image disque source sur OpenNebula en lecture seule via `qemu-nbd`
2. Recopie la table de partitions sur le disque Proxmox (`--init` uniquement)
3. Monte les deux côtés et synchronise les données avec `rsync` partition par partition
4. Réinstalle GRUB (BIOS) ou configure le boot UEFI selon le type de VM détecté
5. Corrige le nom d'interface réseau et active la console série `ttyS0`

## Prérequis

- Exécuté en **root** sur Proxmox
- Accès SSH sans mot de passe vers OpenNebula
- Outils requis sur Proxmox : `qemu-nbd`, `kpartx`, `sfdisk`, `rsync`, `chroot`, `e2fsck`, `tune2fs`
- La VM doit être **arrêtée** sur OpenNebula et sur Proxmox

## Configuration

Copier `config.ini.example` vers `config.ini` (ignoré par git) et renseigner les valeurs :

```ini
[opennebula]
host            = info-pra          # alias SSH vers le serveur OpenNebula
nbd_device      = /dev/nbd2         # device NBD utilisé côté OpenNebula
src_mount_base  = /mnt/onebula-sync-src

[proxmox]
storage         = local-lvm         # storage Proxmox pour les disques
bridge          = interne           # bridge réseau
net_iface       = ens18             # nom d'interface dans la VM migrée
grub_nbd_device = /dev/nbd1         # device NBD local pour la réinstallation GRUB
dst_mount_base  = /mnt/proxmox-sync-dst
```

## Usage

```bash
# 1. Créer la VM Proxmox vide (CPU/RAM/disques) d'après la config OpenNebula
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --create

# 2. Premier passage : recrée les partitions, filesystems, copie tout, installe GRUB
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --init

# 3. Resynchronisations rapides (rsync incrémental, seuls les fichiers modifiés)
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --rsync

# Tout en une commande :
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --create --init

# Diagnostic en lecture seule : état de chaque disque (table de partitions
# source vs destination), pour savoir lesquels ont besoin d'un --init-disk
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --state-disk

# Réinitialise un seul disque (table + filesystems) sans toucher aux autres,
# ex. quand un --init précédent a été interrompu avant ce disque
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --init-disk 2

# Répare un boot BIOS cassé (ajoute une partition BIOS Boot ef02 si besoin)
./migrer-vm-opennebula-vers-proxmox.py vm-glpi-bookworm --reinstall-grub
```

`--init` et `--init-disk` demandent une confirmation explicite (`oui`) avant
de recréer une table de partitions ou un filesystem.

## Structure

```
python/
  migrer-vm-opennebula-vers-proxmox.py   ← script principal
  config.ini                             ← configuration locale (gitignored)
  config.ini.example                     ← modèle de configuration
  lib/
    run.py          ← helpers subprocess (local + SSH)
    config.py       ← chargement config.ini
    opennebula.py   ← interrogation OpenNebula (onevm list/show)
    proxmox.py      ← opérations Proxmox (qm, pvesm, pvesh)
    nbd.py          ← connexion/déconnexion NBD (source + GRUB), alterne les
                      devices NBD source entre disques pour éviter une
                      reconnexion immédiate sur le même device
    partition.py    ← table de partitions, kpartx, comparaison, diagnostic
                      --state-disk
    filesystem.py   ← mkfs, e2fsck, montage
    lvm.py          ← gestion LVM (PV/VG/LV)
    sync.py         ← rsync source → destination
    grub.py         ← installation GRUB + corrections post-migration
    cleanup.py      ← nettoyage garanti (atexit + signaux)
```
