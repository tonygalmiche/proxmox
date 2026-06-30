#!/bin/bash
set -euo pipefail

# Création d'une VM Proxmox vide (CPU/RAM/disques) avec la même configuration
# que la VM correspondante sur OpenNebula. Les disques sont créés vides, à la
# bonne taille : les données seront copiées/importées plus tard par
# migrer-vm-opennebula-vers-proxmox.sh.
# Voir creer-vm-proxmox.md pour le détail du fonctionnement.
#
# Usage: ./creer-vm-proxmox.sh <nom-vm> [bridge-reseau]

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

VM_NAME="${1:-}"
PROXMOX_BRIDGE="${2:-$PROXMOX_BRIDGE_DEFAUT}"

if [ -z "$VM_NAME" ]; then
    echo "Usage: $0 <nom-vm> [bridge-reseau]" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Erreur : la commande 'python3' est requise sur ce serveur Proxmox." >&2
    exit 1
fi

# Arrondi un nombre de Mo au Go supérieur (qm set ... storage:Go n'accepte que des Go entiers)
mb_to_gb_ceil() {
    local mb="$1"
    echo $(( (mb + 1023) / 1024 ))
}

if qm list | awk -v name="$VM_NAME" '$2==name{found=1} END{exit !found}'; then
    PROXMOX_VMID=$(qm list | awk -v name="$VM_NAME" '$2==name{print $1}')
    echo "La VM '$VM_NAME' existe déjà sur Proxmox (VMID=$PROXMOX_VMID). Rien à faire."
    exit 0
fi

# OpenNebula suffixe le nom des VM avec leur ID (ex: vm-dokuwiki-bullseye-203).
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

if [ "${#ON_MATCHES[@]}" -eq 0 ]; then
    echo "Erreur : aucune VM '$VM_NAME' (ou '$VM_NAME-<id>') trouvée sur OpenNebula." >&2
    exit 1
fi

if [ "${#ON_MATCHES[@]}" -gt 1 ]; then
    echo "Erreur : plusieurs VM correspondent à '$VM_NAME' sur OpenNebula, soyez plus précis :" >&2
    printf '%s\n' "${ON_MATCHES[@]}" >&2
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

# CPU (nombre de coeurs) et MEMOIRE (Mo)
IFS=$'\t' read -r ON_VCPU ON_MEMORY_MB <<< "$(echo "$ON_SHOW_XML" | python3 -c '
import sys
import math
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
template = root.find("TEMPLATE")
vcpu = template.findtext("VCPU") or template.findtext("CPU") or "1"
vcpu = max(1, math.ceil(float(vcpu)))
memory = template.findtext("MEMORY", "1024")
print(f"{vcpu}\t{memory}")
')"

# Liste des disques (DISK_ID, IMAGE, SIZE en Mo, TARGET), triée par DISK_ID
mapfile -t ON_DISKS < <(echo "$ON_SHOW_XML" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
template = root.find("TEMPLATE")
disks = template.findall("DISK") if template is not None else []
disks.sort(key=lambda d: int(d.findtext("DISK_ID", "0")))
for d in disks:
    disk_id = d.findtext("DISK_ID", "")
    image = d.findtext("IMAGE", "")
    size = d.findtext("SIZE", "")
    target = d.findtext("TARGET", "")
    print(f"{disk_id}\t{image}\t{size}\t{target}")
')

if [ "${#ON_DISKS[@]}" -eq 0 ]; then
    echo "Erreur : aucun disque trouvé pour la VM '$ON_VM_NAME' sur OpenNebula." >&2
    exit 1
fi

PROXMOX_VMID=$(pvesh get /cluster/nextid)

qm create "$PROXMOX_VMID" \
    --name "$VM_NAME" \
    --memory "$ON_MEMORY_MB" \
    --cores "$ON_VCPU" \
    --cpu host \
    --ostype l26 \
    --scsihw virtio-scsi-pci \
    --net0 "virtio,bridge=$PROXMOX_BRIDGE,firewall=1" \
    >/dev/null

printf '%-30s %-14s %-7s %-12s %-13s %-8s %-25s %-15s %-12s\n' \
    "VM" "OpenNebula_ID" "VCPU" "MEMOIRE(Mo)" "Proxmox_VMID" "DISK_ID" "IMAGE" "OpenNebula(MB)" "Proxmox(GB)"
for line in "${ON_DISKS[@]}"; do
    IFS=$'\t' read -r disk_id image size target <<< "$line"
    size_gb=$(mb_to_gb_ceil "$size")
    qm set "$PROXMOX_VMID" --scsi"$disk_id" "$PROXMOX_STORAGE:$size_gb,iothread=1" >/dev/null
    printf '%-30s %-14s %-7s %-12s %-13s %-8s %-25s %-15s %-12s\n' \
        "$VM_NAME" "$ON_VM_ID" "$ON_VCPU" "$ON_MEMORY_MB" "$PROXMOX_VMID" "$disk_id" "$image" "$size" "$size_gb"
done

qm set "$PROXMOX_VMID" --boot order=scsi0 >/dev/null
