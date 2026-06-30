#!/bin/bash
set -euo pipefail

# Synchronise les données des disques d'une VM depuis OpenNebula vers les disques
# (déjà créés, vides, par creer-vm-proxmox.sh) de la même VM sur Proxmox.
#
# Contrairement à une copie brute de l'image disque, ce script monte la source
# (via qemu-nbd, en lecture seule) et la destination (le volume LVM Proxmox, via
# kpartx) puis fait un rsync au niveau fichier entre les deux. Cela évite de devoir
# stocker une copie complète de l'image sur Proxmox, et permet de relancer le
# script (mode par défaut) pour ne synchroniser que les fichiers modifiés (rapide).
#
# Par défaut, le script ne touche qu'aux données (rsync) : il suppose que la table de
# partitions et les filesystems existent déjà sur le disque Proxmox cible. Pour le tout
# premier passage (disque Proxmox vide, sans partition), utiliser --init : cette option
# recrée la table de partitions et les filesystems en miroir de la source — opération
# destructive, à ne lancer qu'une fois.
#
# Voir synchroniser-vm-opennebula-vers-proxmox.md pour le détail du fonctionnement
# et ses limites.
#
# Usage: ./synchroniser-vm-opennebula-vers-proxmox.sh <nom-vm> [--init]

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

VM_NAME="${1:-}"
INIT=no
[ "${2:-}" = "--init" ] && INIT=yes

if [ -z "$VM_NAME" ]; then
    echo "Usage: $0 <nom-vm> [--init]" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Erreur : ce script doit être exécuté en root (mount/mkfs/chroot)." >&2
    exit 1
fi

if [ "$INIT" = "yes" ]; then
    echo "⚠️  --init va recréer la table de partitions et les filesystems sur les disques"
    echo "    Proxmox de la VM '$VM_NAME' : toute donnée déjà présente sur ces disques sera PERDUE."
    read -r -p "Confirmer ? (oui/non) : " CONFIRM
    if [ "$CONFIRM" != "oui" ]; then
        echo "Annulé."
        exit 1
    fi
fi

for cmd in python3 pvesm qm qemu-img qemu-nbd sfdisk partprobe kpartx blkid blockdev rsync chroot mknod tune2fs e2fsck; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "Erreur : commande '$cmd' manquante sur Proxmox." >&2; exit 1; }
done

# Normalise une sortie `sfdisk -d` en une liste "start,size,type" par partition
# (un par ligne), pour pouvoir comparer deux tables de partitions sans tenir
# compte du nom du device.
normalize_partition_table() {
    awk -F'[, \t]+' '
        /^\// {
            start=""; size=""; type="";
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^start=/) { split($i, a, "="); start = a[2] }
                else if ($i ~ /^size=/) { split($i, a, "="); size = a[2] }
                else if ($i ~ /^type=/) { split($i, a, "="); type = a[2] }
            }
            print start "," size "," type
        }
    '
}

declare -a DST_MOUNTS_TO_CLEANUP=()
CURRENT_PVE_DEV=""

cleanup() {
    local m
    for m in "${DST_MOUNTS_TO_CLEANUP[@]:-}"; do
        [ -n "$m" ] && mountpoint -q "$m" 2>/dev/null && umount "$m" 2>/dev/null || true
    done
    [ -n "$CURRENT_PVE_DEV" ] && kpartx -d "$CURRENT_PVE_DEV" >/dev/null 2>&1 || true
    qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true
    ssh "$OPENNEBULA_HOST" bash -s -- "$NBD_DEVICE" "$SRC_MOUNT_BASE" <<'EOF' >/dev/null 2>&1 || true
NBD="$1"
BASE="$2"
for m in $(mount | awk -v base="$BASE" 'index($3, base) == 1 {print $3}'); do
    umount "$m" 2>/dev/null || true
done
kpartx -d "$NBD" >/dev/null 2>&1 || true
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
EOF
}
trap cleanup EXIT

# Synchronise un disque (un couple source OpenNebula / volume Proxmox).
sync_disk() {
    local disk_id="$1" on_source="$2" pve_volume="$3"

    local pve_dev
    pve_dev=$(pvesm path "$pve_volume")
    CURRENT_PVE_DEV="$pve_dev"

    # 1. Côté OpenNebula : (re)connecte l'image source en lecture seule via NBD,
    #    et expose ses partitions via kpartx (plus fiable que le scan natif du noyau,
    #    qui rate les partitions logiques dans une partition étendue), ou le device
    #    entier s'il n'y a pas de table de partitions.
    local -a src_parts
    mapfile -t src_parts < <(ssh "$OPENNEBULA_HOST" bash -s -- "$on_source" "$NBD_DEVICE" "$SRC_MOUNT_BASE" <<'EOF'
set -euo pipefail
SRC="$1"
NBD="$2"
BASE="$3"
modprobe nbd max_part=16 2>/dev/null || true
# Démonte tout résidu d'un run précédent (sinon kpartx -d / qemu-nbd --disconnect
# échouent silencieusement et laissent /dev/nbd0 dans un état cassé).
for m in $(mount | awk -v base="$BASE" 'index($3, base) == 1 {print $3}'); do
    umount "$m" 2>/dev/null || true
done
kpartx -d "$NBD" >/dev/null 2>&1 || true
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
FORMAT=$(qemu-img info "$SRC" | awk -F': ' '/^file format/{print $2}')
qemu-nbd --read-only --format="$FORMAT" --connect="$NBD" "$SRC"
sleep 1
MAPS=$(kpartx -avs "$NBD" 2>/dev/null | awk '{print $3}')
if [ -n "$MAPS" ]; then
    for m in $MAPS; do echo "/dev/mapper/$m"; done
else
    echo "$NBD"
fi
EOF
)

    if [ "${#src_parts[@]}" -eq 0 ]; then
        echo "Erreur : impossible de connecter/lire $on_source sur $OPENNEBULA_HOST." >&2
        return 1
    fi

    # 2. Côté Proxmox : recopie la table de partitions (uniquement avec --init), puis expose
    #    les partitions du volume LVM via kpartx.
    if [ "$INIT" = "yes" ]; then
        echo "  Copie de la table de partitions ($on_source -> $pve_dev)"
        kpartx -d "$pve_dev" >/dev/null 2>&1 || true
        ssh "$OPENNEBULA_HOST" "sfdisk -d $NBD_DEVICE" 2>/dev/null | sfdisk "$pve_dev" >/dev/null 2>&1 || true
        partprobe "$pve_dev" 2>/dev/null || true
        sleep 1
    fi

    local dst_parts=()
    local -a kpartx_lines
    mapfile -t kpartx_lines < <(kpartx -avs "$pve_dev" 2>/dev/null || true)
    if [ "${#kpartx_lines[@]}" -eq 0 ]; then
        dst_parts=("$pve_dev")
    else
        local l
        for l in "${kpartx_lines[@]}"; do
            dst_parts+=("/dev/mapper/$(awk '{print $3}' <<< "$l")")
        done
    fi

    if [ "${#src_parts[@]}" -ne "${#dst_parts[@]}" ]; then
        echo "Erreur : nombre de partitions différent entre $on_source (${#src_parts[@]}) et $pve_dev (${#dst_parts[@]})." >&2
        return 1
    fi

    # 2bis. Vérification de cohérence de la table de partitions entre source et
    #    destination avant toute copie de données : si elles ne correspondent pas,
    #    on n'effectue aucun rsync.
    local has_table_src=no has_table_dst=no
    if [ "${#src_parts[@]}" -gt 1 ] || [ "${src_parts[0]}" != "$NBD_DEVICE" ]; then
        has_table_src=yes
    fi
    if [ "${#kpartx_lines[@]}" -gt 0 ]; then
        has_table_dst=yes
    fi

    if [ "$has_table_src" != "$has_table_dst" ]; then
        echo "Erreur : présence d'une table de partitions différente entre $on_source (partitionné=$has_table_src) et $pve_dev (partitionné=$has_table_dst). Aucune donnée copiée." >&2
        return 1
    fi

    if [ "$has_table_src" = "yes" ]; then
        local src_table_sig dst_table_sig
        src_table_sig=$(ssh "$OPENNEBULA_HOST" "sfdisk -d $NBD_DEVICE" 2>/dev/null | normalize_partition_table)
        dst_table_sig=$(sfdisk -d "$pve_dev" 2>/dev/null | normalize_partition_table)
        if [ "$src_table_sig" != "$dst_table_sig" ]; then
            echo "Erreur : la table de partitions de $pve_dev ne correspond pas à celle de $on_source. Aucune donnée copiée." >&2
            echo "  Source      : $src_table_sig" >&2
            echo "  Destination : $dst_table_sig" >&2
            echo "  Relancez avec --init pour la recréer (écrase le disque Proxmox), ou vérifiez manuellement." >&2
            return 1
        fi
    else
        local src_dev_size dst_dev_size
        src_dev_size=$(ssh "$OPENNEBULA_HOST" "blockdev --getsize64 $NBD_DEVICE" 2>/dev/null || true)
        dst_dev_size=$(blockdev --getsize64 "$pve_dev" 2>/dev/null || true)
        if [ -z "$src_dev_size" ] || [ "$src_dev_size" != "$dst_dev_size" ]; then
            echo "Erreur : taille de disque différente entre $on_source ($src_dev_size) et $pve_dev ($dst_dev_size). Aucune donnée copiée." >&2
            return 1
        fi
    fi

    mkdir -p "$SRC_MOUNT_BASE" "$DST_MOUNT_BASE"

    local root_dst_mount="" root_part_num="" root_fsuuid=""
    local idx
    for idx in "${!src_parts[@]}"; do
        local src_part="${src_parts[$idx]}"
        local dst_part="${dst_parts[$idx]}"

        local fstype fsuuid
        fstype=$(ssh "$OPENNEBULA_HOST" "blkid -o value -s TYPE '$src_part'" 2>/dev/null || true)
        fsuuid=$(ssh "$OPENNEBULA_HOST" "blkid -o value -s UUID '$src_part'" 2>/dev/null || true)

        if [ "$fstype" = "swap" ]; then
            [ "$INIT" = "yes" ] && mkswap ${fsuuid:+-U "$fsuuid"} "$dst_part" >/dev/null 2>&1
            continue
        fi

        # Partition sans filesystem détecté (ex. conteneur de partition étendue) :
        # ignorée silencieusement, ce n'est pas une anomalie.
        [ -z "$fstype" ] && continue

        if [ "$INIT" = "yes" ]; then
            case "$fstype" in
                ext2|ext3) mkfs."$fstype" -q -F ${fsuuid:+-U "$fsuuid"} "$dst_part" ;;
                ext4)
                    # Le mkfs.ext4 de Proxmox (e2fsprogs plus récent que celui d'origine
                    # sur OpenNebula) active par défaut des fonctionnalités ext4 que GRUB
                    # et/ou le e2fsck embarqué dans l'initramfs de la VM (ancien) ne
                    # supportent pas : "unknown filesystem" côté grub-install/grub-probe
                    # (metadata_csum, 64bit), puis "unsupported feature(s)" côté fsck au
                    # boot (orphan_file). On les désactive systématiquement, même si la
                    # source les a déjà (cas de metadata_csum/64bit ici) : c'est l'outil
                    # GRUB côté Proxmox qui ne les supporte pas, pas la source.
                    mkfs.ext4 -q -F -O ^metadata_csum,^metadata_csum_seed,^64bit,^orphan_file \
                        ${fsuuid:+-U "$fsuuid"} "$dst_part"
                    # mkfs.ext4 -O ^orphan_file n'est pas toujours honoré à la création
                    # (selon la version d'e2fsprogs) : on force le désactivation après
                    # coup via tune2fs, qui agit directement sur le superblock.
                    tune2fs -O ^orphan_file "$dst_part" >/dev/null 2>&1 || true
                    ;;
                xfs)
                    mkfs.xfs -q -f "$dst_part"
                    [ -n "$fsuuid" ] && command -v xfs_admin >/dev/null 2>&1 && xfs_admin -U "$fsuuid" "$dst_part" >/dev/null
                    ;;
                *)
                    echo "  Attention : filesystem '$fstype' non géré automatiquement, partition ignorée." >&2
                    continue
                    ;;
            esac
        fi

        # Vérifie/répare le filesystem ext* avant de le monter : la destination est
        # exposée tour à tour via kpartx (dm) puis, pour GRUB, via une connexion qemu-nbd
        # séparée sur le même disque physique — un défaut de synchronisation entre ces
        # deux chemins d'E/S peut laisser des métadonnées incohérentes d'un run à
        # l'autre ("Structure needs cleaning" côté rsync). e2fsck -fy corrige ça
        # automatiquement (no-op si le filesystem est sain, ex. juste après --init).
        local ec
        case "$fstype" in
            ext2|ext3|ext4)
                e2fsck -fy "$dst_part" >/dev/null 2>&1 && ec=0 || ec=$?
                if [ "$ec" -ge 4 ]; then
                    echo "Erreur : e2fsck n'a pas pu réparer $dst_part (code $ec)." >&2
                    return 1
                fi
                ;;
        esac

        local src_mnt="$SRC_MOUNT_BASE/disk${disk_id}-p${idx}"
        local dst_mnt="$DST_MOUNT_BASE/disk${disk_id}-p${idx}"

        ssh "$OPENNEBULA_HOST" "umount '$src_mnt' 2>/dev/null; mkdir -p '$src_mnt' && mount -o ro '$src_part' '$src_mnt'"
        umount "$dst_mnt" 2>/dev/null || true
        mkdir -p "$dst_mnt"
        mount "$dst_part" "$dst_mnt"
        DST_MOUNTS_TO_CLEANUP+=("$dst_mnt")

        rsync -aHAX --delete --numeric-ids -e ssh "${OPENNEBULA_HOST}:${src_mnt}/" "$dst_mnt/" >/dev/null

        ssh "$OPENNEBULA_HOST" "umount '$src_mnt'"

        if [ -f "$dst_mnt/etc/fstab" ]; then
            root_dst_mount="$dst_mnt"
            root_part_num=$(echo "$dst_part" | grep -oE '[0-9]+$')
            root_fsuuid="$fsuuid"
        fi
    done

    # Démonte tout (côté Proxmox) avant l'étape GRUB : celle-ci utilise une connexion
    # NBD locale dédiée plutôt que les mappings kpartx, donc plus besoin de ces montages.
    local m
    for m in "${DST_MOUNTS_TO_CLEANUP[@]}"; do
        umount "$m" 2>/dev/null || true
    done
    DST_MOUNTS_TO_CLEANUP=()

    # Flush explicite de chaque device de partition kpartx, et du disque parent, AVANT
    # de supprimer les mappings kpartx : sinon des données peuvent rester en cache sur
    # le device de partition (dm-X) et ne jamais atteindre le disque parent avant sa
    # suppression, ce qui fait apparaître des données partielles/obsolètes une fois
    # relu plus tard via qemu-nbd pour GRUB (vu en pratique : fichiers "fantômes" dans
    # /usr/sbin, stat() échouant pour la plupart d'entre eux).
    sync
    for m in "${dst_parts[@]}"; do
        blockdev --flushbufs "$m" 2>/dev/null || true
    done
    blockdev --flushbufs "$pve_dev" 2>/dev/null || true
    sync

    [ "${#kpartx_lines[@]}" -gt 0 ] && kpartx -d "$pve_dev" >/dev/null 2>&1 || true
    CURRENT_PVE_DEV=""

    # 3. Réinstallation de GRUB (BIOS uniquement) si une racine a été synchronisée sur ce disque.
    #
    # GRUB ne sait pas correctement analyser une table de partitions kpartx posée sur un
    # volume LVM (il traite le LV comme un device "diskfilter" et perd le décalage de la
    # partition, ce qui donne "unknown filesystem" même si tout le reste est correct). On
    # contourne ça en exposant le disque Proxmox via qemu-nbd (comme côté OpenNebula) : le
    # noyau y fait alors un scan de partitions standard, que GRUB sait analyser normalement.
    if [ -n "$root_dst_mount" ] && [ -n "$root_part_num" ]; then
        # S'assure que tout ce qui a été écrit via le mapping kpartx (mkfs, rsync) est
        # bien parvenu jusqu'au stockage avant de relire le même device via une
        # connexion qemu-nbd séparée — sinon celle-ci peut voir des données obsolètes.
        sync
        blockdev --flushbufs "$pve_dev" 2>/dev/null || true

        modprobe nbd max_part=16 2>/dev/null || true
        qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true
        qemu-nbd --format=raw --connect="$GRUB_NBD_DEVICE" "$pve_dev"
        partprobe "$GRUB_NBD_DEVICE" 2>/dev/null || true
        sleep 1

        local grub_root_dev="${GRUB_NBD_DEVICE}p${root_part_num}"
        local grub_mnt="$DST_MOUNT_BASE/grub-disk${disk_id}"
        mkdir -p "$grub_mnt"
        mount "$grub_root_dev" "$grub_mnt"

        if [ ! -e "$grub_mnt/etc/fstab" ]; then
            echo "  Attention : $grub_root_dev monté sur $grub_mnt semble vide ou incorrect (pas de /etc/fstab), GRUB ignoré." >&2
            umount "$grub_mnt" 2>/dev/null || true
            qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true
        else
            mount --bind "$grub_mnt" "$grub_mnt"

            # /dev minimal et isolé : si on bind-montait le /dev complet de l'hôte, GRUB y
            # verrait À LA FOIS /dev/nbd1 et le volume LVM d'origine (/dev/pve/..., /dev/mapper/...)
            # qui pointent vers les mêmes octets. Son scan de signatures LVM se raccroche alors
            # au chemin LVM plutôt qu'au device nbd qu'on lui demande, ce qui reproduit le bug
            # "unknown filesystem" qu'on cherche justement à éviter. En isolant /dev à uniquement
            # ce dont GRUB a besoin (le device nbd + quelques nodes standards), cette ambiguïté
            # disparaît.
            mount -t tmpfs -o size=1M,mode=755 tmpfs "$grub_mnt/dev"
            mknod -m 666 "$grub_mnt/dev/null" c 1 3
            mknod -m 666 "$grub_mnt/dev/zero" c 1 5
            mknod -m 444 "$grub_mnt/dev/random" c 1 8
            mknod -m 444 "$grub_mnt/dev/urandom" c 1 9
            mknod -m 600 "$grub_mnt/dev/console" c 5 1
            local nbd_node
            for nbd_node in "$GRUB_NBD_DEVICE" "$GRUB_NBD_DEVICE"p*; do
                [ -e "$nbd_node" ] || continue
                : > "$grub_mnt$nbd_node"
                mount --bind "$nbd_node" "$grub_mnt$nbd_node"
            done

            local fs
            for fs in proc sys; do
                mount --bind "/$fs" "$grub_mnt/$fs"
            done

            # Active la console série (ttyS0) dans l'image, pour pouvoir ensuite utiliser
            # `qm terminal <vmid>` (terminal SSH classique, copier/coller normal) plutôt
            # que la console graphique noVNC (qui ne permet pas de coller un mot de passe).
            # Les images OpenNebula n'ont généralement pas ça par défaut.
            if ! grep -q "console=ttyS0" "$grub_mnt/etc/default/grub" 2>/dev/null; then
                sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 console=tty0 console=ttyS0,115200n8"/' "$grub_mnt/etc/default/grub"
            fi
            mkdir -p "$grub_mnt/etc/systemd/system/getty.target.wants"
            ln -sf /lib/systemd/system/serial-getty@.service \
                "$grub_mnt/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"

            local grub_log
            grub_log=$(mktemp)
            if ! chroot "$grub_mnt" grub-install --target=i386-pc "$GRUB_NBD_DEVICE" >"$grub_log" 2>&1; then
                echo "  Attention : échec de grub-install sur $pve_dev." >&2
                cat "$grub_log" >&2
            fi
            if ! chroot "$grub_mnt" update-grub >"$grub_log" 2>&1; then
                echo "  Attention : échec de update-grub sur $pve_dev." >&2
                cat "$grub_log" >&2
            fi
            rm -f "$grub_log"

            # update-grub a été exécuté dans le chroot avec la racine montée depuis
            # $grub_root_dev (un device NBD temporaire côté hôte) : si grub-probe n'a
            # pas pu résoudre l'UUID (notre /dev minimal isolé n'a pas les symlinks
            # /dev/disk/by-uuid/ habituellement fournis par udev), grub.cfg peut se
            # retrouver avec "root=$grub_root_dev" en dur au lieu d'un UUID portable —
            # inutilisable au démarrage réel de la VM ("does not exist" dans l'initramfs).
            # On corrige explicitement en remplaçant par l'UUID réel de la partition.
            if [ -n "$root_fsuuid" ] && [ -f "$grub_mnt/boot/grub/grub.cfg" ]; then
                sed -i "s#root=$grub_root_dev#root=UUID=$root_fsuuid#g" "$grub_mnt/boot/grub/grub.cfg"
            fi

            for fs in sys proc; do
                umount "$grub_mnt/$fs"
            done
            for nbd_node in "$GRUB_NBD_DEVICE" "$GRUB_NBD_DEVICE"p*; do
                mountpoint -q "$grub_mnt$nbd_node" 2>/dev/null && umount "$grub_mnt$nbd_node"
            done
            umount "$grub_mnt/dev"
            umount "$grub_mnt"
            umount "$grub_mnt"
            qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true

            qm set "$PROXMOX_VMID" --serial0 socket >/dev/null 2>&1 || true
        fi
    fi

    ssh "$OPENNEBULA_HOST" "kpartx -d $NBD_DEVICE >/dev/null 2>&1; qemu-nbd --disconnect $NBD_DEVICE" >/dev/null 2>&1 || true
}

# --- Localisation de la VM sur OpenNebula (même logique que les autres scripts) ---

if ! ON_LIST_XML=$(ssh "$OPENNEBULA_HOST" "onevm list -x" 2>/tmp/onevm_list_err.$$); then
    echo "Erreur : impossible de lister les VM sur OpenNebula." >&2
    cat /tmp/onevm_list_err.$$ >&2
    rm -f /tmp/onevm_list_err.$$
    exit 1
fi
rm -f /tmp/onevm_list_err.$$

mapfile -t ON_ALL_VMS < <(echo "$ON_LIST_XML" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
for vm in root.findall("VM"):
    vmid = vm.findtext("ID", "")
    name = vm.findtext("NAME", "")
    print(f"{vmid}\t{name}")
')

mapfile -t ON_MATCHES < <(printf '%s\n' "${ON_ALL_VMS[@]}" | awk -F'\t' -v name="$VM_NAME" '
    $2 == name || $2 ~ ("^" name "-[0-9]+$") { print }
')

if [ "${#ON_MATCHES[@]}" -ne 1 ]; then
    echo "Erreur : VM OpenNebula '$VM_NAME' introuvable ou ambiguë sur $OPENNEBULA_HOST." >&2
    exit 1
fi

IFS=$'\t' read -r ON_VM_ID ON_VM_NAME <<< "${ON_MATCHES[0]}"

if ! ON_SHOW_XML=$(ssh "$OPENNEBULA_HOST" "onevm show $ON_VM_ID -x" 2>/tmp/onevm_show_err.$$); then
    echo "Erreur : impossible de récupérer la VM ID=$ON_VM_ID sur OpenNebula." >&2
    cat /tmp/onevm_show_err.$$ >&2
    rm -f /tmp/onevm_show_err.$$
    exit 1
fi
rm -f /tmp/onevm_show_err.$$

ON_STATE=$(echo "$ON_SHOW_XML" | python3 -c 'import sys,xml.etree.ElementTree as ET; print(ET.fromstring(sys.stdin.read()).findtext("STATE",""))')
case "$ON_STATE" in
    4|8|9) ;; # STOPPED, POWEROFF, UNDEPLOYED
    *)
        echo "Erreur : la VM '$ON_VM_NAME' n'est pas arrêtée sur OpenNebula (état=$ON_STATE)." >&2
        exit 1
        ;;
esac

mapfile -t ON_DISKS < <(echo "$ON_SHOW_XML" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
template = root.find("TEMPLATE")
disks = template.findall("DISK") if template is not None else []
disks.sort(key=lambda d: int(d.findtext("DISK_ID", "0")))
for d in disks:
    print("\t".join([
        d.findtext("DISK_ID", ""),
        d.findtext("IMAGE", ""),
        d.findtext("SOURCE", ""),
    ]))
')

if [ "${#ON_DISKS[@]}" -eq 0 ]; then
    echo "Erreur : aucun disque trouvé pour '$ON_VM_NAME' sur OpenNebula." >&2
    exit 1
fi

# --- Localisation de la VM + des disques sur Proxmox ---

PROXMOX_VMID=$(qm list | awk -v name="$VM_NAME" '$2==name{print $1}')

if [ -z "$PROXMOX_VMID" ]; then
    echo "Erreur : VM Proxmox '$VM_NAME' introuvable. Lancez d'abord creer-vm-proxmox.sh." >&2
    exit 1
fi

PROXMOX_STATUS=$(qm status "$PROXMOX_VMID" | awk '{print $2}')
if [ "$PROXMOX_STATUS" != "stopped" ]; then
    echo "Erreur : la VM Proxmox '$VM_NAME' (VMID=$PROXMOX_VMID) n'est pas arrêtée (état=$PROXMOX_STATUS)." >&2
    echo "         Monter/écrire sur son disque pendant qu'elle tourne le corromprait (vécu en pratique)." >&2
    echo "         Arrêtez-la d'abord : qm shutdown $PROXMOX_VMID" >&2
    exit 1
fi

mapfile -t PROXMOX_DISK_LINES < <(
    qm config "$PROXMOX_VMID" \
    | grep -E '^(scsi|virtio|ide|sata)[0-9]+:' \
    | grep -v 'media=cdrom' \
    | sort -t: -k1,1V
)

if [ "${#PROXMOX_DISK_LINES[@]}" -ne "${#ON_DISKS[@]}" ]; then
    echo "Erreur : nombre de disques différent entre OpenNebula (${#ON_DISKS[@]}) et Proxmox (${#PROXMOX_DISK_LINES[@]})." >&2
    echo "         Vérifiez la configuration créée par creer-vm-proxmox.sh avant de synchroniser." >&2
    exit 1
fi

PROXMOX_VOLUMES=()
for line in "${PROXMOX_DISK_LINES[@]}"; do
    rest="${line#*: }"
    PROXMOX_VOLUMES+=("${rest%%,*}")
done

START_TS=$(date +%s)
echo "VM '$VM_NAME' : OpenNebula ID=$ON_VM_ID <-> Proxmox VMID=$PROXMOX_VMID (${#ON_DISKS[@]} disque(s), init=$INIT) - début $(date -d "@$START_TS" +%H:%M:%S)"

for i in "${!ON_DISKS[@]}"; do
    IFS=$'\t' read -r disk_id image source <<< "${ON_DISKS[$i]}"
    sync_disk "$disk_id" "$source" "${PROXMOX_VOLUMES[$i]}"
done

END_TS=$(date +%s)
echo "Synchronisation terminée pour la VM '$VM_NAME' (Proxmox VMID=$PROXMOX_VMID) - fin $(date -d "@$END_TS" +%H:%M:%S) (durée $((END_TS - START_TS))s)"
