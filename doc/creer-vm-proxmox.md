# creer-vm-proxmox.sh

Script de création d'une VM **Proxmox** vide, avec la même configuration (CPU, mémoire,
disques) que la VM correspondante sur **OpenNebula**. Les disques créés sont **vides**
(juste à la bonne taille) : les données seront copiées/importées plus tard par
[synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md).

Le script est lancé **depuis le serveur Proxmox**, qui dispose d'un accès SSH complet
(avec clé) vers le serveur OpenNebula.

## Usage

```bash
./creer-vm-proxmox.sh <nom-vm> [bridge-reseau]
```

- `<nom-vm>` : nom de la VM sur OpenNebula (sans le suffixe `-<id>`), ex. `vm-glpi-bookworm`.
- `[bridge-reseau]` : bridge réseau Proxmox à utiliser pour la carte réseau de la VM
  (optionnel, `interne` par défaut — bridge utilisé par les VM Proxmox existantes).

Exemple :

```bash
./creer-vm-proxmox.sh vm-glpi-bookworm interne
```

Pour connaître le bridge utilisé par les VM Proxmox déjà existantes :

```bash
qm config <vmid> | grep -E '^net[0-9]+:'
```

## Configuration

Les paramètres sont centralisés dans [config.sh](config.sh) (sourcé automatiquement par le
script, doit se trouver dans le même dossier) :

- `OPENNEBULA_HOST` : alias/host SSH (dans `~/.ssh/config`) du serveur OpenNebula (`info-pra` par défaut).
- `PROXMOX_STORAGE` : storage Proxmox sur lequel créer les disques (`local-lvm` par défaut).
- `PROXMOX_BRIDGE_DEFAUT` : bridge réseau utilisé si non précisé en paramètre (`interne` par défaut).

## Fonctionnement

### 1. Vérification que la VM n'existe pas déjà sur Proxmox

Le script regarde si une VM portant le nom `<nom-vm>` existe déjà sur Proxmox (`qm list`).
Si c'est le cas, le script **ne fait rien** et s'arrête (code de sortie 0) : il n'écrase
jamais une VM existante.

### 2. Récupération de la configuration sur OpenNebula

Comme pour les autres scripts du workflow, le script recherche la VM sur OpenNebula par
nom exact ou par préfixe `<nom-vm>-<id>` (`onevm list -x`), puis récupère sa configuration
complète
(`onevm show <id> -x`) :

- nombre de vCPU (`VCPU`, ou `CPU` arrondi au supérieur si `VCPU` absent)
- mémoire en Mo (`MEMORY`)
- liste des disques (`DISK_ID`, `IMAGE`, taille en Mo, `TARGET`)

### 3. Attribution d'un VMID Proxmox

Le script demande à Proxmox le prochain VMID libre (`pvesh get /cluster/nextid`) : aucune
correspondance n'est faite avec l'ID OpenNebula, les deux identifiants sont indépendants.

### 4. Création de la VM

```bash
qm create <vmid> --name <nom-vm> --memory <mo> --cores <vcpu> --cpu host \
    --ostype l26 --scsihw virtio-scsi-pci --net0 virtio,bridge=<bridge>,firewall=1
```

### 5. Création des disques vides

Pour chaque disque OpenNebula (triés par `DISK_ID`), un disque vide de la taille
correspondante (arrondie au Go supérieur) est ajouté sur `scsi<DISK_ID>` :

```bash
qm set <vmid> --scsi<disk_id> <storage>:<taille_Go>,iothread=1
```

Le `DISK_ID` OpenNebula est repris tel quel comme index `scsi<N>` Proxmox, pour rester
cohérent avec ce que recherche [synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md)
(appariement disque par disque, par ordre d'index).

Le disque `scsi0` est ensuite défini comme disque de boot (`qm set <vmid> --boot order=scsi0`).

## Limites connues / à vérifier après création

- Le script ne configure pas le type de disque ni les options avancées (cache, ssd, etc.) :
  à ajuster manuellement si besoin avant migration des données.
- La taille des disques est arrondie au Go supérieur (les tailles OpenNebula sont
  généralement des multiples de 1024 Mo, donc sans incidence en pratique).
- Le bridge réseau est à fournir explicitement si ce n'est pas `interne` (utiliser la commande
  `qm config <vmid> | grep net` sur une VM Proxmox existante pour le retrouver).
- La VM n'est pas démarrée par ce script : démarrage manuel (`qm start <vmid>`) une fois
  les disques synchronisés par `synchroniser-vm-opennebula-vers-proxmox.sh`.
