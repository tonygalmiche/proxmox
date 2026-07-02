# Espace disque et LVM sur Proxmox

Comment retrouver la configuration LVM actuelle, l'espace disque réellement disponible,
et comment vérifier avant une migration qu'un disque OpenNebula "énorme" ne va pas
saturer le storage Proxmox.

## Voir la configuration LVM actuelle

```bash
pvs      # Physical Volumes : disque(s) physique(s) membres du VG
vgs      # Volume Group : taille totale et espace encore non-alloué (VFree)
lvs -a   # Logical Volumes : tous les LV (root, swap, pool thin, disques de VM...)
```

`lvs -a -o lv_name,vg_name,lv_size,data_percent` cible les colonnes utiles : taille
**virtuelle** de chaque LV et, pour un pool thin, `data_percent` = espace **réellement**
occupé à l'intérieur du pool.

### Deux niveaux à ne pas confondre

- **Le VG (Volume Group)** — tout l'espace disque physique du serveur. Sur une install
  Proxmox standard, l'installeur découpe le disque dès le départ en `root`, `swap`, et un
  gros LV thin-pool nommé `data` qui occupe quasiment tout le reste. Résultat : `vgs`
  affiche souvent un `VFree` proche de zéro (quelques Mo) — ce n'est **pas** une alerte,
  c'est le comportement normal de l'installeur : il n'y a simplement plus d'espace non
  découpé à donner à un nouveau LV ou à agrandir le pool `data`.
- **Le pool thin `data`** — c'est LUI qui contient les disques de VM. Sa taille est fixée,
  mais il autorise du **sur-provisionnement** : la somme des tailles virtuelles des disques
  de VM peut dépasser la taille du pool, tant que l'espace **réellement écrit** reste en
  dessous. C'est ce `data_percent` (ou l'occupation via `pvesm status`, voir plus bas) qui
  indique le vrai risque de saturation, pas la taille virtuelle des disques.

Analogie : le VG est un terrain déjà entièrement loti (plus de terrain libre à distribuer),
le pool `data` est un immeuble déjà construit sur ce terrain, avec la plupart de ses
appartements vides.

## Topologie disque de proxmox-test : 3 VG séparés, pas d'agrégation

`pvs` sur `proxmox-test` montre 3 PV, mais **dans 3 VG différents** — donc pas de
striping/agrégation LVM entre eux (s'il y en avait, les 3 PV apparaîtraient sous le même
nom de VG) :

```
PV         VG  Fmt  Attr PSize  PFree
/dev/sda3  pve lvm2 a--  <6.11t  4.00m
/dev/sdb   pvf lvm2 a--  <5.46t <5.43t
/dev/sdc   pvg lvm2 a--  <5.46t <5.46t
```

`/etc/pve/storage.cfg` précise le rôle de chaque VG côté Proxmox :

```
lvmthin: local-lvm   → vgname pve   (thinpool data)
lvm:     pvf         → vgname pvf
lvm:     pvg         → vgname pvg
```

Et `config.ini` du script de migration a `storage = local-lvm` → **toutes les VM
créées/synchronisées par ce script atterrissent uniquement sur `pve`/`/dev/sda3`**.
`pvf` et `pvg` sont déclarés comme storages Proxmox utilisables mais ne sont pas
utilisés actuellement (`PFree` ≈ `PSize`, quasiment vides).

Conséquences :
- **Pas de perte totale** en cas de panne d'un seul disque physique : c'est cloisonné
  par VG (perdre `/dev/sdb` ou `/dev/sdc` ne touche pas `pve`).
- **Mais aucune redondance non plus** : chaque VG n'a qu'un seul PV, pas de RAID/mirroring
  (attribut `wz--n-`, pas de flag mirror). Une panne de `/dev/sda3` (VG `pve`) est le
  scénario grave puisque c'est là que sont toutes les VM migrées à ce jour.
- `pvf`/`pvg` pourraient servir à répartir la charge ou héberger des sauvegardes
  (`vzdump`), mais rien n'est configuré en ce sens pour l'instant.

## Voir l'espace disponible par storage Proxmox

```bash
pvesm status
```

Affiche pour chaque storage configuré (`local`, `local-lvm`, ...) : `Total`, `Used`,
`Available` et un pourcentage d'occupation. Fonctionne quel que soit le type de storage
(LVM-thin, dir, ZFS...), contrairement à `lvs` qui suppose du LVM.

C'est cette commande qu'utilise le script de migration (`lib/proxmox.py:get_storage_usage_pct()`)
pour afficher l'occupation avant chaque `--init`/`--rsync` et bloquer si le seuil
`thin_pool_alert_pct` (`config.ini`) est dépassé.

## Vérifier l'usage réel d'un disque source OpenNebula

Un disque qcow2 sur OpenNebula peut afficher une taille virtuelle énorme (quota alloué)
sans que l'espace correspondant soit réellement utilisé. Avant de migrer, comparer :

```bash
# Sur le frontend OpenNebula (ou via ssh depuis Proxmox)
qemu-img info /var/lib/one/datastores/<ds_id>/<image_hash>
du -h /var/lib/one/datastores/<ds_id>/<image_hash>
```

`qemu-img info` donne `virtual size` (taille allouée/quota) et `disk size` (espace
réellement occupé par le fichier qcow2 sur le datastore) — c'est `disk size` (proche du
résultat de `du -h`) qui donne une estimation réaliste de ce qu'un `rsync` va effectivement
copier, pas `virtual size`.

## Exemple d'analyse (migration vm-rsync, 2026-07-02)

`vm-rsync` (OpenNebula ID=228) déclare des disques avec des tailles virtuelles très
supérieures à leur usage réel :

| Disque | Taille virtuelle (quota) | Espace réellement utilisé (source) |
|---|---|---|
| vm-rsync (root) | 20 GiB | négligeable |
| vm-freedom-samba | 6.84 TiB | **2.79 TiB** |
| vm-odoo-home | 0.98 TiB | **336 GiB** |
| **Total à synchroniser** | ~8.02 TiB (virtuel) | **~3.1 TiB (réel)** |

Comparé à la capacité du pool thin `pve/data` au moment de la migration :

| Métrique | Valeur |
|---|---|
| Taille du pool `data` | 5.96 TiB |
| Déjà utilisé (toutes VM) | 294 GiB (4.93%) |
| Espace physique libre | ~5.7 TiB |

Conclusion : malgré des tailles virtuelles cumulées dépassant la capacité du pool
(8.02 TiB déclarés pour 5.96 TiB de pool), les **3.1 TiB réellement nécessaires**
tenaient largement dans les **5.7 TiB** disponibles — la migration a pu être lancée
sans risque. C'est ce calcul (usage réel vs capacité physique libre) qu'il faut refaire
à chaque fois qu'un disque source affiche une taille virtuelle qui semble disproportionnée.

## Voir aussi

- [commandes-proxmox.md](commandes-proxmox.md) — aide-mémoire `qm`/`pvesm`.
- [synchroniser-vm-opennebula-vers-proxmox.md](synchroniser-vm-opennebula-vers-proxmox.md) — doc du script de migration (utilise le seuil `thin_pool_alert_pct` de `config.ini`).
