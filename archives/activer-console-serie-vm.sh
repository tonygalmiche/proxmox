#!/bin/bash
set -euo pipefail

# Active la console série (ttyS0) sur une VM Proxmox déjà migrée depuis OpenNebula,
# pour pouvoir y accéder avec `qm terminal <vmid>` (terminal SSH classique, donc
# copier/coller normal) plutôt que par la console graphique (noVNC).
#
# Nécessaire car les images OpenNebula n'ont généralement pas de console série
# configurée : ni au niveau du noyau (paramètre GRUB console=ttyS0), ni au niveau du
# getty qui doit tourner dessus. Le script monte le disque racine de la VM hors-ligne
# (qemu-nbd + scan de partitions noyau, même méthode que la réinstallation GRUB dans
# synchroniser-vm-opennebula-vers-proxmox.sh) pour faire ces modifications sans avoir
# besoin que la VM démarre.
#
# Usage: ./activer-console-serie-vm.sh <nom-vm> [disque (scsi<N>), 0 par défaut]

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

VM_NAME="${1:-}"
DISK_INDEX="${2:-0}"

if [ -z "$VM_NAME" ]; then
    echo "Usage: $0 <nom-vm> [disque (scsi<N>), 0 par défaut]" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Erreur : ce script doit être exécuté en root (mount, chroot)." >&2
    exit 1
fi

for cmd in qm pvesm qemu-nbd partprobe chroot mknod blkid; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "Erreur : commande '$cmd' manquante." >&2; exit 1; }
done

PROXMOX_VMID=$(qm list | awk -v name="$VM_NAME" '$2==name{print $1}')
if [ -z "$PROXMOX_VMID" ]; then
    echo "Erreur : VM Proxmox '$VM_NAME' introuvable." >&2
    exit 1
fi

PROXMOX_STATUS=$(qm status "$PROXMOX_VMID" | awk '{print $2}')
if [ "$PROXMOX_STATUS" != "stopped" ]; then
    echo "Erreur : la VM '$VM_NAME' (VMID=$PROXMOX_VMID) doit être arrêtée pour modifier son disque." >&2
    exit 1
fi

PVE_VOLUME=$(qm config "$PROXMOX_VMID" | awk -F': ' -v d="scsi${DISK_INDEX}" '$1==d{print $2}' | cut -d, -f1)
if [ -z "$PVE_VOLUME" ]; then
    echo "Erreur : disque scsi${DISK_INDEX} introuvable sur la VM '$VM_NAME' (VMID=$PROXMOX_VMID)." >&2
    exit 1
fi
PVE_DEV=$(pvesm path "$PVE_VOLUME")

MNT="$DST_MOUNT_BASE/serial-console-${PROXMOX_VMID}"

cleanup() {
    for fs in sys proc; do
        umount "$MNT/$fs" 2>/dev/null || true
    done
    for nbd_node in "$GRUB_NBD_DEVICE" "$GRUB_NBD_DEVICE"p*; do
        mountpoint -q "$MNT$nbd_node" 2>/dev/null && umount "$MNT$nbd_node" 2>/dev/null || true
    done
    umount "$MNT/dev" 2>/dev/null || true
    umount "$MNT" 2>/dev/null || true
    umount "$MNT" 2>/dev/null || true
    qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

modprobe nbd max_part=16 2>/dev/null || true
qemu-nbd --disconnect "$GRUB_NBD_DEVICE" >/dev/null 2>&1 || true
qemu-nbd --format=raw --connect="$GRUB_NBD_DEVICE" "$PVE_DEV"
partprobe "$GRUB_NBD_DEVICE" 2>/dev/null || true
sleep 1

# Trouve la partition racine (celle qui contient /etc/fstab) parmi les partitions
# exposées par le scan noyau de $GRUB_NBD_DEVICE.
mkdir -p "$MNT"
ROOT_DEV=""
for part in "$GRUB_NBD_DEVICE"p*; do
    [ -e "$part" ] || continue
    mount -o ro "$part" "$MNT" 2>/dev/null || continue
    if [ -f "$MNT/etc/fstab" ]; then
        ROOT_DEV="$part"
        umount "$MNT"
        break
    fi
    umount "$MNT"
done

if [ -z "$ROOT_DEV" ]; then
    echo "Erreur : aucune partition racine (avec /etc/fstab) trouvée sur $PVE_DEV." >&2
    exit 1
fi

mount "$ROOT_DEV" "$MNT"
mount --bind "$MNT" "$MNT"

# /dev minimal isolé : voir synchroniser-vm-opennebula-vers-proxmox.sh pour la
# justification (évite toute ambiguïté entre le device nbd temporaire et le volume
# LVM d'origine visible via un bind-mount complet du /dev de l'hôte).
mount -t tmpfs -o size=1M,mode=755 tmpfs "$MNT/dev"
mknod -m 666 "$MNT/dev/null" c 1 3
mknod -m 666 "$MNT/dev/zero" c 1 5
mknod -m 444 "$MNT/dev/random" c 1 8
mknod -m 444 "$MNT/dev/urandom" c 1 9
mknod -m 600 "$MNT/dev/console" c 5 1
for nbd_node in "$GRUB_NBD_DEVICE" "$GRUB_NBD_DEVICE"p*; do
    [ -e "$nbd_node" ] || continue
    : > "$MNT$nbd_node"
    mount --bind "$nbd_node" "$MNT$nbd_node"
done
for fs in proc sys; do
    mount --bind "/$fs" "$MNT/$fs"
done

ROOT_FSUUID=$(blkid -o value -s UUID "$ROOT_DEV")

# update-grub tourne avec la racine montée depuis $ROOT_DEV (device NBD temporaire côté
# hôte, pas le device final de la VM) : sans correction, il embarque ce chemin temporaire
# en dur dans grub.cfg (root=/dev/nbdXpY) au lieu d'un UUID portable, et la VM ne démarre
# plus ("does not exist" dans l'initramfs). Voir synchroniser-vm-opennebula-vers-proxmox.sh
# pour le même correctif.
chroot "$MNT" /bin/bash -s -- "$ROOT_DEV" "$ROOT_FSUUID" <<'EOF'
set -e
ROOT_DEV="$1"
ROOT_FSUUID="$2"
if ! grep -q "console=ttyS0" /etc/default/grub; then
    sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"/GRUB_CMDLINE_LINUX_DEFAULT=\"\1 console=tty0 console=ttyS0,115200n8\"/" /etc/default/grub
fi
update-grub
if [ -n "$ROOT_FSUUID" ] && [ -f /boot/grub/grub.cfg ]; then
    sed -i "s#root=$ROOT_DEV#root=UUID=$ROOT_FSUUID#g" /boot/grub/grub.cfg
fi
mkdir -p /etc/systemd/system/getty.target.wants
ln -sf /lib/systemd/system/serial-getty@.service /etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service
EOF

echo "Console série activée dans l'image (GRUB + getty) pour '$VM_NAME' (VMID=$PROXMOX_VMID)."

cleanup
trap - EXIT

qm set "$PROXMOX_VMID" --serial0 socket >/dev/null
echo "VM Proxmox configurée (--serial0 socket). Console graphique (noVNC) toujours disponible en plus."
echo "Démarrez la VM puis utilisez : qm terminal $PROXMOX_VMID"
