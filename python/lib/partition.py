"""
partition.py — Gestion des tables de partitions et mappings kpartx.

Applique une table sfdisk sur un device cible, expose les partitions via
kpartx, et compare les tables source/destination avant toute copie.
"""
import re
import time
from typing import List, Optional, Tuple

from run import run, ssh

# Espace minimal (secteurs) pour accueillir une partition BIOS Boot (ef02).
_MIN_BIOS_BOOT_SECTORS = 64


def apply_table(device: str, sfdisk_dump: str) -> None:
    """Recopie la table de partitions sur device (--init uniquement)."""
    run(["kpartx", "-d", device], check=False)
    # --no-reread : le disque peut être "en cours d'utilisation" par des dm stales
    # du run précédent ; kpartx -avs relira les partitions juste après.
    r = run(["sfdisk", "--no-reread", device], stdin=sfdisk_dump, capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"sfdisk échoué sur {device}:\n{r.stderr}")
    run(["partprobe", device], check=False)
    time.sleep(1)


def kpartx_add(device: str) -> List[str]:
    """Expose les partitions via kpartx. Retourne la liste /dev/mapper/... (ou [device])."""
    r = run(["kpartx", "-avs", device], capture=True, check=False)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return [device]
    return ["/dev/mapper/" + l.split()[2] for l in lines if len(l.split()) >= 3]


def kpartx_remove(device: str) -> None:
    run(["kpartx", "-d", device], check=False)


def normalize(sfdisk_dump: str) -> List[str]:
    """Extrait les tuples start,size,type d'un dump sfdisk pour comparaison."""
    result = []
    for line in sfdisk_dump.splitlines():
        if not line.startswith("/"):
            continue
        # sfdisk peut écrire "start= 2048" (espace après =) ou "start=2048"
        start = size = ptype = ""
        m = re.search(r'\bstart=\s*(\d+)', line)
        if m:
            start = m.group(1)
        m = re.search(r'\bsize=\s*(\d+)', line)
        if m:
            size = m.group(1)
        m = re.search(r'\btype=\s*(\S+?)(?:,|\s|$)', line)
        if m:
            ptype = m.group(1)
        result.append(f"{start},{size},{ptype}")
    return result


def get_remote_dump(host: str, device: str) -> str:
    return ssh(host, f"sfdisk -d {device}", capture=True, check=False).stdout


def get_local_dump(device: str) -> str:
    return run(["sfdisk", "-d", device], capture=True, check=False).stdout


def get_size(device: str) -> int:
    return int(run(["blockdev", "--getsize64", device], capture=True).stdout.strip())


def get_remote_size(host: str, device: str) -> int:
    return int(ssh(host, f"blockdev --getsize64 {device}", capture=True).stdout.strip())


def find_bios_boot_gap(device: str) -> Optional[Tuple[int, int]]:
    """Cherche l'espace libre entre le header GPT (secteur 34) et la 1re
    partition, pour y créer une partition BIOS Boot (ef02) sans toucher
    aux partitions existantes. Retourne (start, end) ou None si le disque
    n'est pas en GPT, a déjà une ef02, ou n'a pas assez de place."""
    dump = get_local_dump(device)
    if "label: gpt" not in dump:
        return None
    if re.search(r'type=\s*21686148-6449-6[Ee]6[Ff]-744[Ee]-656564454649', dump):
        return None  # BIOS Boot Partition déjà présente

    first_start = None
    for line in dump.splitlines():
        if not line.startswith("/dev/"):
            continue
        m = re.search(r'\bstart=\s*(\d+)', line)
        if m:
            start = int(m.group(1))
            if first_start is None or start < first_start:
                first_start = start

    if first_start is None:
        return None

    gap_start = 34
    gap_end = first_start - 1
    if gap_end - gap_start + 1 < _MIN_BIOS_BOOT_SECTORS:
        return None
    return (gap_start, gap_end)


def add_bios_boot_partition(device: str, start: int, end: int) -> None:
    """Ajoute une partition BIOS Boot (ef02) sur l'espace libre [start, end].

    Le header GPT existant a été écrit par util-linux fdisk avec
    FirstUsableLBA=2048 stocké tel quel (alignement 1 MiB), indépendamment
    de tout calcul dynamique. --set-alignment=1 seul ne le fait PAS
    recalculer. Il faut aussi passer --move-main-table=2 (déplace la table
    principale vers sa position actuelle) : cette opération force gdisk à
    recalculer FirstUsableLBA à partir de l'alignement courant, le faisant
    passer de 2048 à 34 et libérant le gap. Vérifié avec sgdisk --pretend :
    sans --move-main-table, --new=0:34:2047 échoue toujours même avec
    alignement=1 ; avec, First usable sector devient 34.
    """
    run(["sgdisk", "--set-alignment=1", "--move-main-table=2",
         f"--new=0:{start}:{end}", "--typecode=0:ef02",
         "--change-name=0:BIOS boot partition", device])
    run(["partprobe", device], check=False)
    time.sleep(1)


def flush(device: str, partitions: List[str]) -> None:
    """Flush tous les buffers avant de démonter kpartx (évite les données fantômes)."""
    run(["sync"])
    for p in partitions:
        run(["blockdev", "--flushbufs", p], check=False)
    run(["blockdev", "--flushbufs", device], check=False)
    run(["sync"])
