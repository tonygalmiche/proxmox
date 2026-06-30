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

- `qm terminal <vmid>` — console **série** dans le terminal SSH courant (copier/coller normal) ; quitter avec `Ctrl+O`. Nécessite `serial0: socket` dans la config VM et `console=ttyS0` actif côté VM — activé automatiquement par [synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md) lors de la réinstallation de GRUB.
- Console graphique (noVNC) : bouton "Console" dans l'interface web Proxmox — **fermer cet onglet avant d'utiliser `qm terminal`**, sinon les deux consoles se disputent le clavier/l'accès au port série.
- `qm monitor <vmid>` — moniteur QEMU (debug bas niveau, pas un shell de la VM).

## Configuration

- `qm config <vmid>` — affiche la configuration complète de la VM (CPU, RAM, disques, réseau...).
- `qm set <vmid> --<option> <valeur>` — modifie une option de config à chaud (ex. `--serial0 socket`, `--scsi0 storage:taille,iothread=1`).
- `qm create <vmid> --name <nom> ...` — crée une nouvelle VM.
- `qm destroy <vmid>` — supprime définitivement une VM et ses disques (⚠️ destructif).

## Disques / stockage

- `pvesh get /cluster/nextid` — prochain VMID libre.
- `pvesm path <storage:volume>` — résout un volume (ex. `local-lvm:vm-104-disk-0`) en chemin bloc réel (ex. `/dev/pve/vm-104-disk-0`).
- `pvesm status` — liste les storages configurés et leur espace libre.

## Réseau

- `qm config <vmid> | grep net` — affiche la config réseau (bridge, MAC...) d'une VM.
- `ip a` (dans la VM) — vérifie les interfaces réellement détectées par le noyau, à comparer avec `/etc/network/interfaces`.
