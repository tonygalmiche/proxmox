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

### Sur l'hôte Proxmox

- `cat /etc/network/interfaces` — configuration réseau persistante de l'hôte (bridges, bonds, VLANs).
- `ip a` / `ip link show` — état des interfaces physiques et virtuelles (up/down, MTU, MAC).
- `ip -br a` — vue condensée (une ligne par interface) de l'état et des IP.
- `ip route` (ou `ip r`) — table de routage, passerelle par défaut.
- `bridge link show` — liste les interfaces rattachées à chaque bridge (vmbr0, etc.).
- `bridge vlan show` — VLANs réellement actifs par port/interface (à comparer avec `bridge-vids` dans `/etc/network/interfaces`).
- `bridge fdb show` — table CAM/MAC des bridges, utile pour savoir où arrive le trafic d'une MAC donnée.
- `brctl show` — vue classique des bridges et de leurs ports (si `bridge-utils` installé).
- `ethtool <iface>` — vitesse, duplex, état du lien physique.
- `ethtool -S <iface>` — statistiques bas niveau de la carte (erreurs, drops...).
- `cat /proc/net/bonding/<bond0>` — état détaillé d'un bond (mode, esclaves actifs).
- `ip neigh` (ou `arp -a`) — table ARP/voisinage.
- `ss -tulpn` — liste les sockets/ports en écoute avec le process associé.
- `ss -s` — statistiques résumées des connexions.
- `tcpdump -i <iface> -n` — capture du trafic en direct sur une interface (ajouter `host <ip>` ou `port <n>` pour filtrer).
- `ping -c4 <ip>` / `mtr <ip>` — test de connectivité et de latence/route (mtr montre chaque saut).
- `journalctl -u networking -e` — logs du service réseau (erreurs au démarrage/reload).
- `ifreload -a` (ifupdown2) — recharge `/etc/network/interfaces` sans tout couper.
- `pve-firewall status` / `pve-firewall compile` — état et vérification des règles du firewall Proxmox.
- `iptables -L -n -v` / `nft list ruleset` — règles firewall actives au niveau du noyau.
- `cat /sys/class/net/<iface>/carrier` — 1/0 : câble branché et lien actif ou non.
- `pvesh get /nodes/<node>/network` — configuration réseau de l'hôte via l'API Proxmox.

### Configurer des VLANs

Deux approches possibles selon le besoin :

**1. VLAN awareness sur le bridge (recommandé pour un trunk vers plusieurs VM)**

- Dans l'UI : `Système > Réseau > Créer/Éditer le bridge`, cocher **VLAN aware**, et optionnellement restreindre la liste dans **VLAN IDs** (ex. `5 10 20 30`).
- Équivalent dans `/etc/network/interfaces` :
  ```
  auto vmbr0
  iface vmbr0 inet manual
      bridge-ports eno1
      bridge-stp off
      bridge-fd 0
      bridge-vlan-aware yes
      bridge-vids 5 10 20 30 40 100 150 200
  ```
- Le bridge devient alors un **trunk** : chaque VM choisit son VLAN individuellement en renseignant le **tag VLAN** sur sa carte réseau (`qm set <vmid> --net0 virtio=<mac>,bridge=vmbr0,tag=<vlan_id>`, ou champ "VLAN Tag" dans l'UI de la carte réseau de la VM).
- Appliquer un changement de `/etc/network/interfaces` sans reboot : `ifreload -a`.
- Vérifier ensuite avec `bridge vlan show` que le VLAN est bien actif sur l'interface.

**2. Une interface VLAN dédiée sur l'hôte (pour donner une IP à l'hôte lui-même dans un VLAN précis)**

- Dans l'UI : `Créer > VLAN`, en indiquant l'interface parente (ex. `eno1` ou `vmbr0`) et le tag (ex. `10`), ce qui crée une interface du type `eno1.10`.
- Équivalent dans `/etc/network/interfaces` :
  ```
  auto eno1.10
  iface eno1.10 inet static
      address 10.0.10.5/24
      gateway 10.0.10.1
  ```
- Utile pour du management réseau ou un bridge dédié à un seul VLAN (`vmbr0v10` par exemple), par opposition au trunk multi-VLAN de l'option 1.

**Points d'attention**

- Le switch physique en amont du port doit être configuré en **trunk** (802.1Q) pour laisser passer les VLANs tagués, sinon rien n'arrivera même si la conf Proxmox est correcte.
- Après toute modification de `/etc/network/interfaces`, préférer `ifreload -a` (ifupdown2) à un redémarrage réseau complet, pour éviter de couper l'accès SSH à l'hôte.
- `bridge vlan show` reflète l'état réel du noyau — à utiliser en cas de doute plutôt que de se fier uniquement au fichier de config.

### Recommandation pour une PME (~20 VM multi-VLAN)

- Un seul **bridge trunk VLAN-aware** (`vmbr0`), tag VLAN posé individuellement sur chaque VM — pas de bridge par VLAN.
- Segmenter par **rôle** (management, serveurs internes, DMZ, stockage/backup...), pas au hasard.
- **Firewall Proxmox en deny-by-default**, avec security groups par rôle ; le routage inter-VLAN doit passer par un firewall dédié (physique ou VM type pfSense/OPNsense), jamais en direct entre interfaces.
- VLAN management jamais trunké vers la DMZ ni les VM non-admin.
- **Une seule carte réseau physique suffit** pour porter tous les VLANs (c'est le principe du trunk). Une 2e carte n'est utile que pour la **redondance** (bonding/LACP) ou pour isoler physiquement un flux lourd (stockage/backup) — pas pour séparer les VLANs.
- Une VM pare-feu inter-VLAN (pfSense/OPNsense) n'a pas besoin d'une carte physique dédiée non plus — elle passe par le même trunk avec une interface virtuelle taguée par VLAN — mais comme tout le trafic inter-VLAN dépend d'elle, une carte en bonding (redondance) devient plus importante que pour une VM classique.
