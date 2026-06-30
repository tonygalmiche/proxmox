# synchroniser-vm-opennebula-vers-proxmox.sh

Script de synchronisation des **données** des disques d'une VM, depuis **OpenNebula**
vers les disques (déjà créés, vides) de la même VM sur **Proxmox**.

Le script localise et vérifie lui-même la VM des deux côtés (état OpenNebula, état
Proxmox, concordance du nombre/de la taille des disques) avant toute synchronisation :
voir [Pré-requis](#pré-requis) et l'étape 3 ci-dessous. L'approche retenue (montage +
`rsync` au niveau fichier) évite de copier l'image disque brute complète :

- pas besoin d'espace disque supplémentaire sur Proxmox pour stocker une copie de l'image
  OpenNebula (problème bloquant initial) ;
- les synchronisations (mode par défaut) sont rapides car seuls les fichiers modifiés
  sont retransférés (algorithme de delta de `rsync`).

## Principe

Plutôt que de copier l'image disque brute (fichier complet), le script :

1. monte l'image source OpenNebula en lecture seule sur le serveur OpenNebula
   (via `qemu-nbd`) ;
2. expose les partitions du disque Proxmox cible (volume LVM déjà créé par
   [creer-vm-proxmox.sh](creer-vm-proxmox.md)) via `kpartx` ;
3. recrée la table de partitions et les filesystems sur le disque Proxmox (uniquement
   avec `--init`, au tout premier passage) ;
4. monte les deux côtés et fait un `rsync -aHAX --delete` entre les deux montages, partition
   par partition ;
5. réinstalle GRUB (BIOS) sur le disque contenant la racine (`/etc/fstab`), puisque la copie
   fichier par fichier ne récupère pas le secteur de boot (MBR) de l'image d'origine.

## Pré-requis

- La VM doit déjà exister sur Proxmox avec ses disques vides à la bonne taille
  (cf. [creer-vm-proxmox.sh](creer-vm-proxmox.md)).
- La VM doit être arrêtée sur OpenNebula (`STOPPED`, `POWEROFF` ou `UNDEPLOYED`)
  **et** sur Proxmox (`qm status` = `stopped`) — vérifié par le script, qui s'arrête en
  erreur sinon. Monter/écrire sur le disque Proxmox pendant que la VM tourne dessus
  corrompt le filesystem (vécu en pratique : erreurs `Structure needs cleaning` côté
  `rsync` au passage suivant, le disque étant écrit en parallèle par deux acteurs
  différents).
- Outils requis sur Proxmox : `python3`, `pvesm`, `qemu-img`, `qemu-nbd`, `sfdisk`,
  `partprobe`, `kpartx`, `blkid`, `blockdev`, `rsync`, `chroot`, `e2fsck` (et `xfs_admin`
  si des partitions XFS sont utilisées). Le module noyau `nbd` doit pouvoir être chargé
  (`modprobe nbd max_part=16`) — utilisé localement sur Proxmox pour la réinstallation
  de GRUB (`/dev/nbd1` par défaut), en plus de son usage côté OpenNebula (`/dev/nbd0`).
- Le script doit être exécuté en `root` (mount, mkfs, chroot).
- Le fichier [config.sh](config.sh) doit être présent dans le même dossier (paramètres
  `OPENNEBULA_HOST`, `NBD_DEVICE`, `SRC_MOUNT_BASE`, `DST_MOUNT_BASE`).

## Usage

```bash
./synchroniser-vm-opennebula-vers-proxmox.sh <nom-vm> [--init]
```

- **Mode par défaut** (sans option) : ne touche ni à la table de partitions ni aux
  filesystems, se contente de monter les partitions existantes et de lancer/relancer le
  `rsync` (rapide, ne transfère que les changements). C'est le mode à utiliser pour toutes
  les resynchronisations.
- **`--init`** (tout premier passage uniquement) : recrée la table de partitions et les
  filesystems sur les disques Proxmox (en miroir de la source), avant de copier
  l'intégralité des données. ⚠️ Opération **destructive** pour le disque Proxmox cible
  (normalement vide à ce stade) — à ne lancer qu'une seule fois, avant toute autre
  synchronisation. Une confirmation (`oui`/`non`) est demandée avant de poursuivre.

Exemple : initialiser puis resynchroniser plusieurs fois avant la bascule finale :

```bash
./synchroniser-vm-opennebula-vers-proxmox.sh vm-glpi-bookworm --init   # premier passage (destructif, complet)
./synchroniser-vm-opennebula-vers-proxmox.sh vm-glpi-bookworm          # resynchronisations rapides
./synchroniser-vm-opennebula-vers-proxmox.sh vm-glpi-bookworm          # ... autant de fois que nécessaire
```

## Sortie

En usage normal (sans erreur), la sortie tient en 2 lignes (début et fin, avec heure et
durée) : `grub-install`/`update-grub` et `rsync` sont silencieux (sortie affichée
uniquement en cas d'échec). Exemple :

```
VM 'vm-dokuwiki-bullseye' : OpenNebula ID=203 <-> Proxmox VMID=104 (1 disque(s), init=no) - début 14:32:05
Synchronisation terminée pour la VM 'vm-dokuwiki-bullseye' (Proxmox VMID=104) - fin 14:32:48 (durée 43s)
```

## Détail du fonctionnement, par disque

Pour chaque disque (`DISK_ID` OpenNebula ↔ volume Proxmox `scsi<DISK_ID>` correspondant,
appariés par ordre d'index — voir [creer-vm-proxmox.sh](creer-vm-proxmox.md)) :

1. **Connexion NBD côté OpenNebula** : tout montage résiduel d'un run précédent (sous
   `SRC_MOUNT_BASE`) est d'abord démonté — sinon `kpartx -d`/`qemu-nbd --disconnect`
   échouent silencieusement et laissent `/dev/nbd0` dans un état cassé (erreurs I/O sur
   toute lecture ultérieure, vécu en pratique suite à une interruption de script). Puis
   `qemu-nbd --read-only --connect=/dev/nbd0 <source>`, détection du format de l'image
   (`qemu-img info`) et exposition des partitions via `kpartx -avs /dev/nbd0` (plus fiable
   que le scan natif du noyau, qui peut rater les partitions logiques dans une partition
   étendue), ou le disque entier `/dev/nbd0` s'il n'y a pas de table de partitions
   (ex. disque de swap dédié).
2. **Préparation côté Proxmox** (avec `--init` uniquement) : la table de partitions est
   recopiée depuis la source (`sfdisk -d` / `sfdisk`), puis exposée via `kpartx -av` sur le
   volume LVM Proxmox (cette exposition `kpartx` a lieu à chaque passage, `--init` ou non).
3. **Vérification de cohérence des tables de partitions** : avant toute copie de données,
   le script compare la table de partitions de la source et celle de la destination
   (taille/type de chaque partition, via `sfdisk -d`, ou taille du disque entier en
   l'absence de table de partitions). **Si elles ne correspondent pas, le script s'arrête
   en erreur sans faire le moindre `rsync`** — utile pour détecter un disque Proxmox pas
   encore initialisé (`--init` manquant), ou désynchronisé suite à un changement côté
   OpenNebula.
4. **Pour chaque partition** :
   - type de filesystem et UUID détectés côté source via `blkid` ;
   - si c'est une partition `swap` : pas de copie de données, juste `mkswap -U <uuid>`
     côté Proxmox (avec `--init` uniquement) pour conserver le même UUID (cohérence avec
     `fstab`) ;
   - sinon (`ext2`/`ext3`/`ext4`/`xfs`) : le filesystem est recréé côté Proxmox avec le
     **même UUID** que la source (`mkfs.<type> -U <uuid>` / `xfs_admin -U`), pour que
     `/etc/fstab` et la configuration GRUB de la VM restent valides sans modification.
     Pour `ext4`, plusieurs fonctionnalités récentes sont explicitement désactivées
     (`mkfs.ext4 -O ^metadata_csum,^metadata_csum_seed,^64bit,^orphan_file`) :
     - `metadata_csum`/`64bit` (même si la source les a déjà) font échouer
       `grub-install`/`grub-probe` côté Proxmox avec `unknown filesystem`, même quand
       tout le reste (partition, offset, données) est correct — l'outil GRUB du Proxmox
       de test ne les supporte pas, indépendamment de la source ;
     - `orphan_file` (activée par défaut par un `e2fsprogs` plus récent sur Proxmox que
       celui d'origine sur OpenNebula) fait échouer le `fsck` exécuté par l'initramfs
       au démarrage de la VM (`unsupported feature(s)`, boot bloqué en `(initramfs)`).
       Cette feature n'est pas toujours désactivable via `mkfs.ext4 -O ^orphan_file` à
       la création (selon la version d'`e2fsprogs`) : le script force sa désactivation
       après coup avec `tune2fs -O ^orphan_file`, qui agit directement sur le
       superblock.
   - avant le montage, un `e2fsck -fy` est exécuté sur les partitions `ext2`/`ext3`/`ext4`
     côté Proxmox : le disque est exposé tour à tour via `kpartx` (pour le `rsync`) puis,
     pour GRUB, via une connexion `qemu-nbd` séparée sur le même disque physique — un
     défaut de synchronisation entre ces deux chemins d'E/S peut laisser des métadonnées
     incohérentes d'un passage à l'autre (`Structure needs cleaning` côté `rsync` au
     passage suivant). `e2fsck -fy` répare ça automatiquement (no-op si le filesystem est
     déjà sain, par exemple juste après `--init`) ; si la réparation échoue (code retour
     ≥ 4), le script s'arrête en erreur sur ce disque.
   - les deux partitions sont montées (source en lecture seule sur OpenNebula, destination
     en local sur Proxmox) et synchronisées avec `rsync -aHAX --delete --numeric-ids`.
5. **Réinstallation de GRUB et activation de la console série** : si une partition synchronisée contient un `/etc/fstab`
   (donc la racine du système), le script réinstalle GRUB pour que le disque Proxmox
   soit bootable. Le disque est exposé une seconde fois via `qemu-nbd`
   (`GRUB_NBD_DEVICE`, `/dev/nbd1` par défaut, distinct du `NBD_DEVICE` côté
   OpenNebula) plutôt que d'utiliser directement le mapping `kpartx` du volume LVM :
   le noyau y fait un scan de partitions standard (comme un disque normal). Avant
   cette (re)connexion, un `sync` + `blockdev --flushbufs` du disque Proxmox force
   l'écriture de tout ce qui a été fait via le mapping `kpartx` (mkfs, rsync) sur le
   stockage, pour éviter que la connexion `qemu-nbd` (un accès séparé au même device)
   ne lise des données pas encore synchronisées. Une vérification (présence de
   `/etc/fstab` une fois monté) confirme que la partition récupérée via `/dev/nbd1pN`
   est bien la bonne avant de poursuivre — sinon GRUB est juste ignoré avec un
   avertissement plutôt que de planter en plein chroot.

   Dans le `chroot` (bind-mount de la racine sur elle-même + `/proc`, `/sys`), le
   `/dev` n'est **pas** un bind-mount du `/dev` complet de l'hôte : un `/dev` minimal
   (tmpfs) est créé, exposant uniquement `$GRUB_NBD_DEVICE` (et ses partitions) plus
   quelques nodes standards (`null`, `zero`, `random`, `urandom`, `console`). Bonne
   pratique d'isolation (évite toute ambiguïté avec d'autres devices visibles côté
   hôte), même si la cause réelle de `unknown filesystem` rencontrée pendant la mise
   au point de ce script était ailleurs : voir les fonctionnalités ext4 décrites à
   l'étape 4 (`metadata_csum`/`64bit`).

   `grub-install --target=i386-pc` et `update-grub` tournent ensuite dans cet
   environnement isolé. Comme la racine est montée depuis `$grub_root_dev` (le device
   NBD temporaire côté hôte, pas le device final de la VM), `update-grub` peut
   embarquer ce chemin temporaire en dur dans `grub.cfg` (`root=/dev/nbd1pN`) au lieu
   d'un UUID portable — `grub-probe` n'a pas pu résoudre l'UUID car le `/dev` minimal
   isolé n'a pas les symlinks `/dev/disk/by-uuid/` qu'udev fournirait normalement. Le
   script corrige `grub.cfg` après coup (`sed`) pour remplacer cette référence
   temporaire par `root=UUID=<uuid réel>`, sans quoi la VM ne démarre pas
   (`/dev/nbd1p1 does not exist` dans l'initramfs). Un échec de `grub-install`/
   `update-grub` n'interrompt pas le script (juste un avertissement) : à vérifier/
   corriger manuellement avant de démarrer la VM.

   Avant de regénérer `grub.cfg` (`update-grub`), la console série (`ttyS0`) est
   activée dans l'image : ajout de `console=ttyS0,115200n8` à `GRUB_CMDLINE_LINUX_DEFAULT`
   (`/etc/default/grub`) et activation d'un `getty` dessus
   (`serial-getty@ttyS0.service`). Les images OpenNebula n'ont généralement pas ça par
   défaut. Côté Proxmox, `qm set <vmid> --serial0 socket` est appliqué juste après. Une
   fois la VM démarrée, `qm terminal <vmid>` donne alors un terminal SSH classique
   (copier/coller normal du mot de passe), à utiliser de préférence à la console
   graphique noVNC — **fermer l'onglet noVNC avant** d'utiliser `qm terminal`, sinon les
   deux consoles se disputent l'accès au port série (voir
   [commandes-proxmox.md](commandes-proxmox.md)).
6. **Nettoyage** : démontage des deux côtés, suppression des mappings `kpartx`,
   déconnexion des NBD côté OpenNebula et côté Proxmox (GRUB).

## Limites connues

- **BIOS/MBR uniquement** : la réinstallation de GRUB suppose un boot BIOS classique
  (`grub-install <disque>`). Les VM en UEFI (partition ESP `vfat`) ne sont pas gérées
  automatiquement et nécessiteront une intervention manuelle.
- **Filesystems gérés** : `ext2`/`ext3`/`ext4` et `xfs` uniquement. Toute autre partition
  (type inconnu ou non géré) est ignorée avec un avertissement — à traiter manuellement.
- **Un seul disque NBD à la fois** : les disques d'une même VM sont synchronisés
  séquentiellement (un seul `/dev/nbd0` réutilisé), donc pas de parallélisation.
- **`--init` écrase les disques Proxmox** : à ne lancer qu'une fois les disques vides
  effectivement créés (voir [creer-vm-proxmox.sh](creer-vm-proxmox.md)), et une seule fois
  par VM (les passages suivants doivent être faits sans `--init`).
- **VM doit rester arrêtée sur OpenNebula et sur Proxmox** pendant toute la phase de
  synchronisation (y compris les resynchronisations), vérifié par le script : monter
  l'image ou écrire sur le disque d'une VM en cours d'exécution n'est pas sûr (et corrompt
  le filesystem, vécu en pratique).
- Ce script ne démarre pas la VM sur Proxmox : démarrage manuel (`qm start <vmid>`) une
  fois la dernière synchronisation effectuée juste avant la bascule.
