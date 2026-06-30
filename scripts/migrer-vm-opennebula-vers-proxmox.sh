#!/bin/bash
set -euo pipefail

# Migration d'une VM depuis OpenNebula vers Proxmox.
# Voir migrer-vm-opennebula-vers-proxmox.md pour le détail des étapes.
#
# Usage: ./migrer-vm-opennebula-vers-proxmox.sh <nom-vm>

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=config.sh
source "$SCRIPT_DIR/config.sh"

VM_NAME="${1:-}"

if [ -z "$VM_NAME" ]; then
    echo "Usage: $0 <nom-vm>" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Erreur : la commande 'python3' est requise sur ce serveur Proxmox." >&2
    exit 1
fi

# Convertit une taille Proxmox (ex: 32G, 512M, 1T) en Mo (entier)
size_to_mb() {
    local size="$1"
    local value unit
    value=$(echo "$size" | grep -oE '^[0-9]+')
    unit=$(echo "$size" | grep -oE '[A-Za-z]+$' || true)
    case "$unit" in
        K|k) echo $(( value / 1024 )) ;;
        M|m|"") echo "$value" ;;
        G|g) echo $(( value * 1024 )) ;;
        T|t) echo $(( value * 1024 * 1024 )) ;;
        *)
            echo "Unité de taille inconnue '$unit' (valeur '$size')" >&2
            exit 1
            ;;
    esac
}

# OpenNebula suffixe le nom des VM avec leur ID (ex: vm-dokuwiki-bullseye-203).
# OpenNebula 5.12 ne supporte que la sortie XML (-x), pas de JSON (-j/--json).
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

ON_STATE=$(echo "$ON_SHOW_XML" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
print(root.findtext("STATE", ""))
')

declare -A ON_STATE_NAMES=(
    [0]=INIT [1]=PENDING [2]=HOLD [3]=ACTIVE [4]=STOPPED
    [5]=SUSPENDED [6]=DONE [8]=POWEROFF [9]=UNDEPLOYED
    [10]=CLONING [11]=CLONING_FAILED
)
ON_STATE_NAME="${ON_STATE_NAMES[$ON_STATE]:-UNKNOWN($ON_STATE)}"

case "$ON_STATE" in
    4|8|9) ;; # STOPPED, POWEROFF, UNDEPLOYED : la VM ne tourne pas
    *)
        echo "Erreur : la VM '$ON_VM_NAME' n'est pas arrêtée sur OpenNebula (état: $ON_STATE_NAME)." >&2
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

PROXMOX_VMID=$(qm list | awk -v name="$VM_NAME" '$2==name{print $1}')

if [ -z "$PROXMOX_VMID" ]; then
    echo "Erreur : aucune VM nommée '$VM_NAME' trouvée sur Proxmox (qm list)." >&2
    exit 1
fi

mapfile -t PROXMOX_DISKS < <(
    qm config "$PROXMOX_VMID" \
    | grep -E '^(scsi|virtio|ide|sata)[0-9]+:' \
    | grep -v 'media=cdrom' \
    | sort -t: -k1,1V
)

if [ "${#PROXMOX_DISKS[@]}" -eq 0 ]; then
    echo "Erreur : aucun disque trouvé pour la VM Proxmox '$VM_NAME' (VMID=$PROXMOX_VMID)." >&2
    exit 1
fi

PROXMOX_IF=()
PROXMOX_SIZE_MB=()
for line in "${PROXMOX_DISKS[@]}"; do
    iface="${line%%:*}"
    rest="${line#*: }"
    volume="${rest%%,*}"
    size_param=$(echo "$rest" | grep -oE 'size=[0-9]+[A-Za-z]*' | cut -d= -f2 || true)
    if [ -z "$size_param" ]; then
        echo "Erreur : impossible de déterminer la taille du disque '$iface' ($volume)." >&2
        exit 1
    fi
    PROXMOX_IF+=("$iface")
    PROXMOX_SIZE_MB+=("$(size_to_mb "$size_param")")
done

if [ "${#ON_DISKS[@]}" -ne "${#PROXMOX_IF[@]}" ]; then
    echo "Erreur : nombre de disques différent (OpenNebula: ${#ON_DISKS[@]}, Proxmox: ${#PROXMOX_IF[@]})." >&2
    exit 1
fi

MISMATCH=0
printf '%-22s %-14s %-18s %-13s %-8s %-25s %-15s %-10s %-15s %-10s\n' \
    "VM" "OpenNebula_ID" "OpenNebula_Etat" "Proxmox_VMID" "DISK_ID" "IMAGE" "OpenNebula(MB)" "INTERFACE" "Proxmox(MB)" "STATUT"
for i in "${!ON_DISKS[@]}"; do
    IFS=$'\t' read -r disk_id image on_size target <<< "${ON_DISKS[$i]}"
    pve_iface="${PROXMOX_IF[$i]}"
    pve_size="${PROXMOX_SIZE_MB[$i]}"

    if [ "$on_size" -eq "$pve_size" ]; then
        statut="OK"
    else
        statut="MISMATCH"
        MISMATCH=1
    fi
    printf '%-22s %-14s %-18s %-13s %-8s %-25s %-15s %-10s %-15s %-10s\n' \
        "$VM_NAME" "$ON_VM_ID" "$ON_STATE_NAME" "$PROXMOX_VMID" "$disk_id" "$image" "$on_size" "$pve_iface" "$pve_size" "$statut"
done

if [ "$MISMATCH" -ne 0 ]; then
    echo "Erreur : des écarts de taille ont été détectés entre OpenNebula et Proxmox." >&2
    exit 1
fi
