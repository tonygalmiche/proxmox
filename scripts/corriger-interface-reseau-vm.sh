#!/bin/bash
set -euo pipefail

# Corrige le nom d'interface réseau dans /etc/network/interfaces d'une VM Proxmox
# déjà migrée depuis OpenNebula, hors-ligne (disque monté, sans démarrer la VM).
#
# OpenNebula et Proxmox n'attribuent pas le même nom d'interface réseau (udev/systemd
# nomme l'interface selon le bus PCI virtuel) : une VM dont l'interface s'appelait
# "ens3" ou "eth0" sous OpenNebula se retrouve par exemple en "ens18" sous Proxmox
# (premier NIC virtio = net0). /etc/network/interfaces référence encore l'ancien nom,
# l'interface ne se lève donc plus au boot ("Failed to start Raise network interfaces").
#
# Usage: ./corriger-interface-reseau-vm.sh <nom-vm> [nouvelle-interface] [disque (scsi<N>), 0 par défaut]
# Par défaut, la nouvelle interface est "ens18" (1er NIC virtio sur Proxmox).

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

VM_NAME="${1:-}"
NEW_IFACE="${2:-ens18}"
DISK_INDEX="${3:-0}"

if [ -z "$VM_NAME" ]; then
    echo "Usage: $0 <nom-vm> [nouvelle-interface] [disque (scsi<N>), 0 par défaut]" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Erreur : ce script doit être exécuté en root (mount)." >&2
    exit 1
fi

for cmd in qm pvesm qemu-nbd partprobe; do
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
    echo "         Arrêtez-la d'abord : qm shutdown $PROXMOX_VMID" >&2
    exit 1
fi

PVE_VOLUME=$(qm config "$PROXMOX_VMID" | awk -F': ' -v d="scsi${DISK_INDEX}" '$1==d{print $2}' | cut -d, -f1)
if [ -z "$PVE_VOLUME" ]; then
    echo "Erreur : disque scsi${DISK_INDEX} introuvable sur la VM '$VM_NAME' (VMID=$PROXMOX_VMID)." >&2
    exit 1
fi
PVE_DEV=$(pvesm path "$PVE_VOLUME")

MNT="$DST_MOUNT_BASE/fix-network-${PROXMOX_VMID}"

cleanup() {
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

IFACES_FILE="$MNT/etc/network/interfaces"
if [ ! -f "$IFACES_FILE" ]; then
    echo "Erreur : $IFACES_FILE introuvable." >&2
    exit 1
fi

# Noms d'interface référencés (hors "lo"), déduits des lignes "auto"/"iface"/"allow-hotplug".
mapfile -t OLD_IFACES < <(awk '/^(auto|iface|allow-hotplug)[ \t]+/{print $2}' "$IFACES_FILE" | sort -u | grep -v '^lo$' || true)

if [ "${#OLD_IFACES[@]}" -eq 0 ]; then
    echo "Aucune interface (hors lo) trouvée dans $IFACES_FILE, rien à corriger."
    exit 0
fi

cp "$IFACES_FILE" "$IFACES_FILE.bak-$(date +%Y%m%d%H%M%S)"

for old_iface in "${OLD_IFACES[@]}"; do
    if [ "$old_iface" = "$NEW_IFACE" ]; then
        continue
    fi
    sed -i "s/\b$old_iface\b/$NEW_IFACE/g" "$IFACES_FILE"
    echo "Interface '$old_iface' -> '$NEW_IFACE' dans /etc/network/interfaces."
done

echo "Terminé pour '$VM_NAME' (VMID=$PROXMOX_VMID). Démarrez la VM pour vérifier : qm start $PROXMOX_VMID"
