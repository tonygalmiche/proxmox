# Commandes Proxmox utiles

Aide-mémoire des commandes `qm`/`pvesm`/... les plus utilisées pour ce projet, une ligne
par commande.

## VM : état et cycle de vie

- `qm list` — liste toutes les VM (VMID, nom, état).
- `qm status <vmid>` — état d'une VM (`running`/`stopped`).
- `qm start <vmid>` — démarre la VM.
- `qm shutdown <vmid>` — arrêt propre (ACPI) de la VM.
- `qm stop <vmid>` — arrêt forcé (équivalent débrancher), à utiliser si `shutdown` ne répond pas.
- `qm reset <vmid>` — redémarrage forcé (équivalent bouton reset).

## Console

- `qm set <vmid> --serial0 socket` — ajoute une interface série virtuelle à une VM qui n'en a pas (erreur `unable to find a serial interface` sur `qm terminal`). Modification persistante dans `/etc/pve/qemu-server/<vmid>.conf` ; nécessite un **arrêt complet puis redémarrage de la VM** (un reboot depuis l'OS invité ne suffit pas). Retrait avec `qm set <vmid> --delete serial0`. Il faut aussi que l'OS invité ait `console=ttyS0` actif (GRUB) pour que la console série affiche quelque chose.
- `qm terminal <vmid>` — console **série** dans le terminal SSH courant (copier/coller normal) ; quitter avec `Ctrl+O`. Nécessite `serial0: socket` dans la config VM et `console=ttyS0` actif côté VM — activé automatiquement par [synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md) lors de la réinstallation de GRUB.
- Console graphique (noVNC) : bouton "Console" dans l'interface web Proxmox — **fermer cet onglet avant d'utiliser `qm terminal`**, sinon les deux consoles se disputent le clavier/l'accès au port série.
- `qm monitor <vmid>` — moniteur QEMU (debug bas niveau, pas un shell de la VM).
- **`qm terminal` reste bloqué sans prompt login (Entrée ne fait rien)** — le getty série n'écoute pas dans la VM. Diagnostic via noVNC : `systemctl status serial-getty@ttyS0.service`. Si `inactive (dead)` : `systemctl enable --now serial-getty@ttyS0.service` (effet immédiat, pas de reboot). Vérifier aussi `cat /proc/cmdline | grep -o 'console=[^ ]*'` — si vide, ajouter `console=tty0 console=ttyS0,115200n8` à `GRUB_CMDLINE_LINUX_DEFAULT` dans `/etc/default/grub`, puis `update-grub` (nécessite un redémarrage complet de la VM pour s'appliquer).

## Configuration

- `qm config <vmid>` — affiche la configuration complète de la VM (CPU, RAM, disques, réseau...).
- `qm set <vmid> --<option> <valeur>` — modifie une option de config à chaud (ex. `--serial0 socket`, `--scsi0 storage:taille,iothread=1`).
- `qm create <vmid> --name <nom> ...` — crée une nouvelle VM.
- `qm destroy <vmid>` — supprime définitivement une VM et ses disques (⚠️ destructif).

## Disques / stockage

- `pvesh get /cluster/nextid` — prochain VMID libre.
- `pvesm path <storage:volume>` — résout un volume (ex. `local-lvm:vm-104-disk-0`) en chemin bloc réel (ex. `/dev/pve/vm-104-disk-0`).
- `pvesm status` — liste les storages configurés (type, espace libre/total).
- `pvesm list <storage>` — liste tous les volumes d'un storage (ex. `local-lvm`) : nom, VMID propriétaire, format, taille. Équivalent CLI de l'onglet "Contenu du disque" d'un storage dans l'interface web.
- `qm config <vmid> | grep -E 'scsi|virtio|ide|sata'` — liste les disques d'une VM précise (slot, storage:volume, taille, options).

### `raw` vs `qcow2`

- **LVM/LVM-thin** (ex. `local-lvm`) ne supporte que `raw` : une LV est un volume bloc brut, sans notion de format de fichier. C'est aussi le format le plus performant (pas de table d'allocation à consulter comme dans `qcow2`, donc moins de latence/CPU par I/O) — normal et recommandé pour ce type de storage, rien à changer.
- `qcow2` n'a d'intérêt que sur un storage **fichier** (`dir`, NFS) qui n'a pas nativement de thin-provisioning/snapshot : `qcow2` apporte ces fonctionnalités au niveau du fichier. Sur LVM-thin ou ZFS, ces fonctionnalités existent déjà au niveau du storage — empiler `qcow2` par-dessus serait redondant et plus lent.

## Réseau

- `qm config <vmid> | grep net` — affiche la config réseau (bridge, MAC...) d'une VM.
- `ip a` (dans la VM) — vérifie les interfaces réellement détectées par le noyau, à comparer avec `/etc/network/interfaces`.
