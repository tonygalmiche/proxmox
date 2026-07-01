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

# Démontage des résidus sur notre point de montage.
for m in $(mount | awk -v b="$BASE" 'index($3,b)==1{print $3}'); do
    umount "$m" 2>/dev/null || true
done

# Déconnecte notre NBD EN PREMIER, avant vgchange.
# vgchange -an envoie des FLUSH sur les PV LVM (ex: nbd2p4) ce qui corrompt
# l'état kernel du device NBD → sfdisk retourne I/O error même après reconnexion.
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
# Supprime les entrées partition kernel stales (nbd2p*) laissées par le run précédent.
partx -d "$NBD" 2>/dev/null || true
sleep 1

# Nettoyage global APRÈS déconnexion : plus de risque I/O sur notre NBD.
vgchange -an 2>/dev/null || true
for n in /dev/nbd0 /dev/nbd1 /dev/nbd2 /dev/nbd3 /dev/nbd4 /dev/nbd5; do
    [ "$n" != "$NBD" ] && [ -b "$n" ] && kpartx -d "$n" >/dev/null 2>&1 || true
done
sleep 1

FORMAT=$(qemu-img info "$SRC" | awk -F': ' '/^file format/{print $2}')
qemu-nbd --read-only --format="$FORMAT" --connect="$NBD" "$SRC"

# qemu-nbd --connect est un daemon : fork + exit 0 immédiatement.
# La connexion peut échouer silencieusement si le kernel NBD n'a pas libéré le device.
# On vérifie que la taille du device est non nulle (>0 = connecté).
CONNECTED=0
for i in 1 2 3 4 5 6; do
    sleep 1
    SIZE=$(blockdev --getsize64 "$NBD" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt 0 ]; then
        CONNECTED=1
        break
    fi
done
if [ "$CONNECTED" -eq 0 ]; then
    echo "ERREUR: qemu-nbd n'a pas connecté $NBD après 6s (device size=0)" >&2
    exit 1
fi

SFDISK_DUMP=$(sfdisk -d "$NBD" 2>/dev/null || true)
echo "__SFDISK_START__"
echo "$SFDISK_DUMP"
echo "__SFDISK_END__"

# Désactive les VG auto-activés par udev APRÈS sfdisk.
vgchange -an 2>/dev/null || true

# Liste des partitions tirée du dump sfdisk (pas de ls /dev/nbd*p*
# qui peut contenir des entrées stales d'un run précédent).
PARTS=$(printf '%s\n' "$SFDISK_DUMP" | awk '/^\/dev\// {print $1}')
if [ -n "$PARTS" ]; then
    printf '%s\n' "$PARTS"
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
