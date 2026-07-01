"""
nbd.py — Connexion/déconnexion NBD (local Proxmox pour GRUB, distant OpenNebula pour source).

remote_connect() renvoie le dump sfdisk et la liste des partitions.
Nettoie systématiquement les résidus des runs précédents avant de se connecter
(PV UUID en double, mappings kpartx stales, zombie NBD).
"""
import time
from typing import Tuple, List

from run import run, ssh_script


# --- NBD local (Proxmox, pour la réinstallation GRUB) ---

def local_connect(device: str, pve_dev: str) -> None:
    run(["modprobe", "nbd", "max_part=16"], check=False)
    run(["qemu-nbd", "--disconnect", device], check=False)
    run(["qemu-nbd", "--format=raw", f"--connect={device}", pve_dev])
    run(["partprobe", device], check=False)
    time.sleep(1)


def local_disconnect(device: str) -> None:
    run(["qemu-nbd", "--disconnect", device], check=False)


# --- NBD distant (OpenNebula, pour la source) ---

_CONNECT_SCRIPT = r"""
set -euo pipefail
SRC="$1" NBD="$2" BASE="$3"

modprobe nbd max_part=16 2>/dev/null || true

# Démontage des résidus + nettoyage de tous les NBD stales.
# Nécessaire pour éviter les PV UUID en double (même image montée sur un
# NBD différent lors d'un run précédent), qui font échouer vgchange -ay.
for m in $(mount | awk -v b="$BASE" 'index($3,b)==1{print $3}'); do
    umount "$m" 2>/dev/null || true
done
vgchange -an 2>/dev/null || true
for n in /dev/nbd0 /dev/nbd1 /dev/nbd2 /dev/nbd3 /dev/nbd4 /dev/nbd5; do
    [ -b "$n" ] && kpartx -d "$n" >/dev/null 2>&1 || true
done
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true

FORMAT=$(qemu-img info "$SRC" | awk -F': ' '/^file format/{print $2}')
qemu-nbd --read-only --format="$FORMAT" --connect="$NBD" "$SRC"
sleep 2

echo "__SFDISK_START__"
sfdisk -d "$NBD" 2>/dev/null || true
echo "__SFDISK_END__"

kpartx -avs "$NBD" >/dev/null 2>&1 || true
sleep 0.5
NBD_BASE=$(basename "$NBD")
MAPS=$(ls /dev/mapper/${NBD_BASE}p* 2>/dev/null || true)
if [ -n "$MAPS" ]; then
    for m in $MAPS; do echo "$m"; done
else
    echo "$NBD"
fi
"""

_DISCONNECT_SCRIPT = r"""
NBD="$1" BASE="$2"
for m in $(mount | awk -v b="$BASE" 'index($3,b)==1{print $3}'); do
    umount "$m" 2>/dev/null || true
done
vgchange -an 2>/dev/null || true
kpartx -d "$NBD" >/dev/null 2>&1 || true
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
"""


def remote_connect(host: str, source: str, nbd_device: str,
                   mount_base: str) -> Tuple[str, List[str]]:
    """Retourne (sfdisk_dump, liste_partitions)."""
    r = ssh_script(host, _CONNECT_SCRIPT, source, nbd_device, mount_base, capture=True)

    sfdisk_lines: List[str] = []
    partitions: List[str] = []
    in_sfdisk = False

    for line in r.stdout.splitlines():
        if line == "__SFDISK_START__":
            in_sfdisk = True
        elif line == "__SFDISK_END__":
            in_sfdisk = False
        elif in_sfdisk:
            sfdisk_lines.append(line)
        elif line.strip():
            partitions.append(line.strip())

    if not partitions:
        raise RuntimeError(f"Impossible de connecter {source} sur {host} via {nbd_device}.")

    return "\n".join(sfdisk_lines), partitions


def remote_disconnect(host: str, nbd_device: str, mount_base: str) -> None:
    ssh_script(host, _DISCONNECT_SCRIPT, nbd_device, mount_base, check=False)
