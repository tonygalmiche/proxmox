# Migrer des VM d'un serveur Proxmox vers un autre

## Contexte

Contrairement à la migration OpenNebula → Proxmox (voir `migrer-vm-opennebula-vers-proxmox.py`), il n'est pas nécessaire de développer un script pour migrer des VM entre deux serveurs Proxmox : Proxmox fournit des outils standards pour cela.

Deux besoins différents :
1. Migration initiale d'une VM d'un serveur Proxmox vers un autre.
2. Resynchronisation périodique des mêmes VM, sans retransférer l'intégralité des disques (plusieurs To en jeu).

Contrainte : les deux serveurs Proxmox ne doivent **pas** être regroupés en cluster (le second cluster est temporaire).

## Vérification du stockage

Le type de stockage sous-jacent conditionne la méthode de resync incrémentale disponible. Vérifier avec :

```bash
ssh root@<serveur> "pvesm status; cat /etc/pve/storage.cfg"
```

Sur `proxmox-test`, le stockage est en **LVM / LVM-thin** (`local-lvm` en `lvmthin`, `pvf`/`pvg` en `lvm` classique), et non en ZFS.

Cette distinction est importante :
- **ZFS** : `zfs send -i` permet un envoi incrémental natif entre deux snapshots. Proxmox l'exploite directement via `pvesr` (Storage Replication), mais **uniquement entre nœuds d'un même cluster**.
- **LVM-thin** : supporte les snapshots, mais Proxmox n'a pas d'équivalent officiel "send incrémental" packagé pour ce backend. Il existe bien `thin_delta` au niveau device-mapper pour calculer les blocs modifiés entre deux snapshots, mais rien de standard côté Proxmox — ce serait à redévelopper.
- **LVM classique (thick)** : snapshots peu adaptés à un usage long terme (dégradation des performances).

→ Dans notre cas (LVM-thin, pas de cluster), `pvesr` n'est pas utilisable.

## 1. Migration initiale

Options standards, sans développement :

- **Datacenter Manager (PDM)** : outil officiel Proxmox pour gérer plusieurs clusters/nœuds indépendants et faire de la migration à distance (`qm remote-migrate`) via GUI ou CLI.
- **Backup PBS → restore sur l'autre nœud** (voir ci-dessous).
- **Export/import manuel** via SSH (`qm migrate` en mode remote, ou vzdump + restore) : fonctionne mais recopie tout le disque, à réserver à une migration ponctuelle.

## 2. Resynchronisation périodique incrémentale (méthode recommandée)

**Proxmox Backup Server (PBS)** est la solution standard adaptée à ce contexte (LVM-thin, pas de cluster, gros volumes) :

- PBS ne dépend pas du type de stockage sous-jacent : l'incrémental se fait au niveau du contenu du disque virtuel via un mécanisme de *dirty bitmap* au moment du backup, que le stockage soit LVM, LVM-thin, ZFS ou autre.
- Chaque backup après le premier ne transfère et ne stocke que les blocs modifiés (déduplication + compression).
- Fonctionnement :
  1. Installer une instance PBS (peut tourner sur une petite VM/LXC dédiée).
  2. Connecter le serveur Proxmox source à PBS comme storage de backup.
  3. Planifier des jobs de backup incrémentaux (GUI Proxmox, planification native).
  4. Sur le serveur cible, restaurer/synchroniser régulièrement depuis PBS (`qmrestore` planifié via cron ou job Proxmox) — le transfert PBS → cible reste rapide car basé sur les chunks déjà dédupliqués.
- Avantage : 100 % outillage officiel Proxmox, gestion via GUI, aucun développement nécessaire.

### Alternatives écartées

- **`pvesr` (ZFS replication)** : nécessite ZFS + cluster, non applicable ici.
- **`qm migrate` / export-import répétés** : pas incrémental, recopie tout le disque à chaque fois — à éviter vu les volumes en jeu (plusieurs To).
- **LVM-thin snapshots + `thin_delta`** : techniquement possible mais nécessiterait un script maison, donc plus "standard sans développement".

## Migrer d'un stockage à un autre (conversion de format)

Migrer une VM entre deux stockages de types différents (ex. LVM → ZFS) est **standard et sans problème** : que ce soit via `qm migrate` ou via backup PBS + restore, Proxmox convertit automatiquement le format du disque virtuel pour l'adapter au stockage cible. Aucun développement nécessaire.

Point important : passer un seul des deux serveurs en ZFS ne suffit pas à débloquer `pvesr`, qui exige ZFS **des deux côtés** et un **même cluster**. Tant que ces deux conditions ne sont pas réunies, PBS reste la méthode de resync incrémentale, quel que soit le type de stockage.

## Cas ZFS → ZFS : intérêt du `zfs send/receive` natif

Si le serveur source **et** le serveur destination sont tous les deux en ZFS, une option supplémentaire devient disponible, **indépendamment de tout cluster** : `zfs send/receive` en natif (snapshots + envoi différentiel via SSH), scriptable simplement en cron.

- Intérêt par rapport à PBS : plus direct/rapide sur de très gros volumes (pas de couche de chunking/déduplication à traverser), pas d'infrastructure supplémentaire à maintenir (pas de serveur PBS).
- Compromis : moins "clé en main" que PBS — pas de GUI de planification ni de gestion de rétention/pruning intégrée, il faut gérer soi-même la rotation des snapshots et les clés SSH entre les deux hôtes.

## Plan de test envisagé

Trois scénarios à tester dans l'ordre, du plus proche de l'existant au plus optimisé :

1. **LVM → LVM** (configuration actuelle, `proxmox-test`) : PBS pour la resync incrémentale (voir section ci-dessus).
2. **LVM → ZFS** (nouveau serveur reinstallé en ZFS, source encore en LVM) : migration initiale sans souci (conversion de format automatique). Pour la resync, `pvesr` reste indisponible (source non-ZFS) → PBS toujours de mise.
3. **ZFS → ZFS** (serveur définitif, les deux côtés en ZFS) : deux options pour la resync incrémentale à comparer : PBS (confort de gestion) ou `zfs send/receive` natif (performance brute sur gros volumes, sans cluster).

## Prochaine étape

Mettre en place PBS : installation, jobs de backup planifiés côté source, restore programmé côté serveur cible. Puis, une fois le scénario ZFS → ZFS atteint, comparer PBS et `zfs send/receive` natif en conditions réelles.

## Décision finale pour le scénario 1 (LVM → LVM) : script sur mesure

En pratique, PBS a été écarté pour le scénario LVM → LVM : il nécessite un stockage tiers (datastore PBS) pour recevoir au moins un backup complet, or les deux serveurs Proxmox disponibles n'ont chacun tout juste la place pour leurs propres VM (voir détail dans [synchroniser-vm-proxmox-lvm-vers-proxmox-lvm.md](synchroniser-vm-proxmox-lvm-vers-proxmox-lvm.md)).

`lvmsync` (outil tiers basé sur `thin_delta`, transfert des seuls blocs modifiés sans lecture intégrale) a aussi été envisagé puis écarté : limité au LVM-thin, projet peu maintenu, pas de conscience du filesystem.

**Décision retenue** : développer un script sur mesure,
[migrer-vm-proxmox-vers-proxmox.py](../python/migrer-vm-proxmox-vers-proxmox.py), sur le même principe que
[migrer-vm-opennebula-vers-proxmox.py](../python/migrer-vm-opennebula-vers-proxmox.py) (montage des partitions des deux côtés + `rsync` direct via SSH, sans stockage intermédiaire), mais très simplifié pour une première version :

- pas de dépendance au dossier `lib/` (fichier unique) ;
- pas de NBD : la source étant déjà un volume Proxmox (LVM/LVM-thin), ses partitions sont exposées directement via `kpartx` sur le serveur source (SSH) ;
- pas de `config.ini` : `source_host` et `vm_name` passés en argument ;
- filesystems gérés : ext4, xfs, swap uniquement ;
- `--create` crée la VM destination (CPU/RAM/disques vides) d'après la config de la VM source ;
- non géré pour l'instant (à ajouter si besoin après tests) : GRUB/UEFI, LVM interne à la VM.

Usage :

```bash
./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --create  # crée la VM destination (disques vides)
./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --init    # premier passage (destructif, complet)
./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --rsync   # resynchronisations rapides
```

Pré-requis : les deux VM arrêtées. Le script sera étendu au fil des problèmes rencontrés en test.

## Première VM de test : `vm-passerelle-proxmox` (sur `proxmox-test`)

Configuration relevée (`qm config`) :

| Paramètre | Valeur |
|---|---|
| VMID | 100 |
| CPU / RAM | 2 cores / 4096 Mo |
| Disque | `scsi0` : `pvf:vm-100-disk-0`, 32G — storage **`pvf` (LVM classique, pas thin)** |
| Réseau | `net0` : bridge `tous_vlans`, VLAN tag 30 (accès extérieur/production) — `net1` : bridge `interne` (réseau interne isolé) |
| Autres | `serial0: socket` déjà configuré ; `unused0: local-lvm:vm-100-disk-0` résiduel, non utilisé, à ignorer ; `ide2` = ISO d'installation (cdrom, exclu automatiquement par le script) |

Rôle de la VM : passerelle réseau entre l'extérieur (VLAN 30 sur `tous_vlans`, bridge trunk raccordé à une interface physique) et un réseau interne isolé (bridge `interne`, `bridge-ports none` — **aucune interface physique rattachée**) utilisé par les autres VM du serveur.

⚠️ **`vm-passerelle-proxmox` est la seule VM raccordée à `tous_vlans` (donc visible/joignable depuis l'extérieur).** Sa copie sur le nouveau serveur devra donc, elle aussi, se connecter à `tous_vlans` (VLAN 30) pour jouer son rôle de passerelle — il ne peut pas y avoir deux instances actives simultanément sur ce réseau (conflit IP/MAC, rôle de passerelle ambigu). **La VM source doit être arrêtée avant de démarrer la copie** (contrainte déjà appliquée par le script : `--create`, `--init` et `--rsync` vérifient tous les trois que la VM source est arrêtée, et refusent de continuer sinon).

Les VM suivantes à migrer après celle-ci seront, elles, uniquement raccordées au réseau interne (`interne`) — sans lien vers `tous_vlans`. Ce sont ces VM-là qui pourront être testées sur le nouveau serveur (copie connectée uniquement au réseau interne recréé) **sans toucher à la production**, la VM source correspondante restant simplement arrêtée le temps du `--init`/`--rsync` (comme l'exige le script) mais sans impact visible puisqu'elle n'est jamais exposée à l'extérieur.

Points de vigilance avant de démarrer la copie de `vm-passerelle-proxmox` :
- recréer le bridge `interne` (et `tous_vlans` si besoin) sur le nouveau serveur, avec la même configuration (`bridge-ports none` pour `interne`) ;
- arrêter la VM de production avant de démarrer la copie, le temps du test ;
- le disque source est sur `pvf` (LVM classique) : le script gère aussi bien LVM que LVM-thin, donc pas d'impact particulier ici.

## Bridges réseau à créer sur le nouveau serveur (`proxmox-2`)

Déjà fait sur `proxmox-2` (carte trunk : `enp2s0f0`, commentée `#TRUNK` dans `/etc/network/interfaces`) :

```bash
# tous_vlans (trunk, mêmes VLANs que proxmox-test)
cat >> /etc/network/interfaces << 'EOF'

auto enp2s0f0
iface enp2s0f0 inet manual

auto tous_vlans
iface tous_vlans inet manual
	bridge-ports enp2s0f0
	bridge-stp off
	bridge-fd 0
	bridge-vlan-aware yes
	bridge-vids 5 10 20 30 40 100 150 200 250
EOF

# interne (isolé, aucun câblage requis)
cat >> /etc/network/interfaces << 'EOF'

auto interne
iface interne inet manual
	bridge-ports none
	bridge-stp off
	bridge-fd 0
EOF

ifreload -a
```

Équivalent GUI : **Système → Réseau → Create → Linux Bridge**, sans IP, `tous_vlans` en VLAN aware avec `bridge-ports enp2s0f0`, `interne` sans bridge port ; puis **Apply Configuration**.

Vérification : `ip -br link show tous_vlans interne` (doivent être `UP`) et `bridge vlan show tous_vlans` (doit lister les VLANs 5/10/20/30/40/100/150/200/250).
