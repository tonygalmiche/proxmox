# Synchroniser des VM entre deux Proxmox LVM/LVM-thin (via Proxmox Backup Server)

Premier scénario du plan décrit dans
[synchroniser-vm-proxmox-vers-proxmox.md](synchroniser-vm-proxmox-vers-proxmox.md) :
serveur source et serveur destination tous les deux en stockage LVM/LVM-thin, sans
cluster Proxmox entre les deux.

## ⚠️ Point bloquant : PBS écarté, décision finale = script sur mesure

L'approche PBS décrite ci-dessous nécessite un **stockage tiers** (datastore PBS) pour
recevoir au moins un backup complet. Or les deux serveurs Proxmox disponibles n'ont
chacun **tout juste la place pour leurs propres VM**, sans espace disque supplémentaire
pour un troisième stockage de plusieurs To. PBS est donc **écarté** pour ce cas d'usage.

Alternative envisagée : **`lvmsync`**, outil tiers qui utilise `thin_delta` pour ne
transférer que les blocs modifiés entre deux snapshots LVM-thin, sans lire/checksummer
l'intégralité des données (contrairement à `rsync`) ni passer par un stockage
intermédiaire. Intérêt réel si la phase de scan `rsync` s'avère être le goulot
d'étranglement sur des volumes de plusieurs To. Écarté cependant car :
- limité au LVM-thin uniquement (ne couvre pas `pvf`/`pvg` en LVM classique) ;
- projet tiers peu maintenu, non packagé, dépendance Ruby à installer manuellement ;
- opère au niveau bloc brut (pas de conscience du filesystem), donc moins de contrôle
  sur la cohérence (fsck, réinstallation GRUB) que l'approche déjà éprouvée ci-dessous.

**Décision retenue** : développer un script sur mesure, sur le même principe que
[synchroniser-vm-opennebula-vers-proxmox.sh](synchroniser-vm-opennebula-vers-proxmox.md)
(montage des partitions des deux côtés via `qemu-nbd`/`kpartx` + `rsync` direct entre les
deux serveurs Proxmox, sans stockage intermédiaire). Cette approche :
- ne nécessite aucun espace disque supplémentaire (transfert direct source → destination) ;
- réutilise une base de code déjà maîtrisée et éprouvée (gestion cohérence filesystem,
  réinstallation GRUB, resynchronisations incrémentales) ;
- couvre aussi bien LVM-thin que LVM classique.

La suite de ce document (mise en place PBS) est conservée pour référence/comparaison,
mais n'est **plus la méthode retenue** pour ce scénario.

## Principe (approche PBS — non retenue, voir ci-dessus)

Le stockage étant LVM-thin (pas de `zfs send` possible, pas de `pvesr`), on passe par
**Proxmox Backup Server (PBS)** :

1. Le serveur source sauvegarde régulièrement les VM vers PBS (backup incrémental au
   niveau bloc, dirty-bitmap : seuls les blocs modifiés depuis le dernier backup sont
   transférés).
2. Le serveur destination restaure régulièrement depuis PBS (les deux serveurs Proxmox
   sont chacun de simples clients de PBS, pas besoin de cluster entre eux).

```
[Proxmox source] --backup--> [PBS] <--restore-- [Proxmox destination]
```

## Pré-requis

- Une instance PBS accessible en réseau depuis les deux serveurs Proxmox (peut tourner
  dans une VM/LXC dédiée, y compris sur l'un des deux serveurs Proxmox eux-mêmes si les
  ressources le permettent — à éviter en production, préférable sur une machine tierce).
- Espace disque sur PBS suffisant pour l'historique de backups voulu (déduplication +
  compression réduisent fortement le volume réel par rapport à la somme brute des
  disques des VM).
- Accès root SSH sur les deux serveurs Proxmox et sur PBS.

## 1. Installer Proxmox Backup Server

Sur la machine dédiée à PBS (Debian) :

```bash
# Ajouter le dépôt PBS (no-subscription) puis installer
echo "deb http://download.proxmox.com/debian/pbs bookworm pbs-no-subscription" \
  > /etc/apt/sources.list.d/pbs.list
wget https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg \
  -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg
apt update
apt install proxmox-backup-server
```

Interface web disponible sur `https://<ip-pbs>:8007`.

## 2. Créer le datastore PBS

Dans l'interface PBS : **Datastore → Add Datastore**, choisir un chemin de stockage
(disque dédié recommandé) et un nom (ex. `datastore-vm`).

## 3. Créer un utilisateur/token PBS pour chaque serveur Proxmox

Dans PBS : **Configuration → Access Control → API Token**, créer un token par serveur
Proxmox (source et destination), avec les permissions :
- `Datastore.Backup` pour le serveur source (écrire des backups) ;
- `Datastore.Read` (+ éventuellement `Datastore.Backup` si restore inversé un jour) pour
  le serveur destination (lire/restaurer).

Noter l'ID et le secret du token (affiché une seule fois).

## 4. Connecter PBS comme storage sur les deux serveurs Proxmox

Sur **chaque** serveur Proxmox (source et destination), dans l'interface Proxmox VE :
**Datacenter → Storage → Add → Proxmox Backup Server**, renseigner :
- adresse du serveur PBS ;
- datastore (`datastore-vm`) ;
- fingerprint du certificat PBS (visible dans PBS : **Dashboard → Certificate**) ;
- ID/secret du token API créé à l'étape précédente.

Ou en CLI sur chaque Proxmox :

```bash
pvesm add pbs pbs-vm \
  --server <ip-pbs> \
  --datastore datastore-vm \
  --username <token-id>@pbs \
  --password <token-secret> \
  --fingerprint <fingerprint>
```

## 5. Planifier le backup côté serveur source

Sur le serveur Proxmox **source** : **Datacenter → Backup → Add**, sélectionner :
- storage : `pbs-vm` ;
- VM(s) à sauvegarder ;
- mode : `Snapshot` (pas d'arrêt de la VM) ;
- planification : fréquence adaptée au volume de données et à la bande passante
  disponible (ex. quotidien) — après le premier backup complet, chaque exécution est
  incrémentale et donc rapide même sur de gros disques.

## 6. Premier restore complet côté serveur destination

Une fois au moins un backup présent sur PBS, sur le serveur **destination** :

```bash
# Lister les backups disponibles
pvesm list pbs-vm

# Restaurer une VM (choisir un VMID libre ou identique si pas de conflit)
qmrestore pbs-vm:backup/vm/<vmid>/<timestamp> <nouveau-vmid> --storage local-lvm
```

Ou via l'interface : **PBS storage → Content → sélectionner le backup → Restore**.

## 7. Resynchronisations suivantes

Deux approches possibles côté destination, selon si on veut garder la VM en marche
entre deux syncs :

- **Restore complet répété** (`qmrestore` avec le même VMID, en écrasant la précédente
  restauration) : simple, mais nécessite d'arrêter la VM côté destination pendant le
  restore si elle avait été démarrée pour test. Chaque restore ne transfère que les
  chunks non déjà présents localement (déduplication PBS), donc rapide après le premier
  transfert complet.
- **Automatiser via cron** un script appelant `qmrestore` à intervalle régulier, pour ne
  pas dépendre d'une action manuelle avant la bascule finale.

Exemple de restore régulier planifié (cron sur le serveur destination) :

```bash
# /etc/cron.d/sync-vm-depuis-pbs
0 2 * * * root qmrestore pbs-vm:backup/vm/<vmid>/latest <vmid> --storage local-lvm >> /var/log/sync-vm-pbs.log 2>&1
```

⚠️ Ne pas restaurer sur une VM en cours d'exécution côté destination (mêmes précautions
que pour la synchronisation OpenNebula → Proxmox, voir
[synchroniser-vm-opennebula-vers-proxmox.md](synchroniser-vm-opennebula-vers-proxmox.md)) :
arrêter la VM cible avant chaque restore si elle a été démarrée entre deux syncs.

## Bascule finale

Le jour de la bascule : dernier backup côté source (VM arrêtée pour un backup/restore
sans delta manqué), dernier restore côté destination, puis démarrage de la VM sur le
serveur destination.

## Points de vigilance

- Le VMID doit être cohérent entre les deux serveurs (ou remappé explicitement au
  restore) pour éviter les collisions avec des VM déjà existantes sur la destination.
- Le mapping réseau (bridge, VLAN) peut différer entre les deux serveurs : vérifier/
  adapter la configuration réseau de la VM après restore (voir
  [configuration-reseau.md](configuration-reseau.md)).
- Vérifier l'espace disponible sur le datastore PBS au fil du temps (rétention des
  anciens backups à configurer : **Datastore → Prune & GC**).
