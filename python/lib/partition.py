"""
partition.py — Gestion des tables de partitions et mappings kpartx.

Applique une table sfdisk sur un device cible, expose les partitions via
kpartx, et compare les tables source/destination avant toute copie.
"""
import re
import time
from typing import List

from run import run, ssh


def apply_table(device: str, sfdisk_dump: str) -> None:
    """Recopie la table de partitions sur device (--init uniquement)."""
    run(["kpartx", "-d", device], check=False)
    r = run(["sfdisk", device], stdin=sfdisk_dump, capture=True, check=False)
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
        start = size = ptype = ""
        for field in re.split(r"[,\s]+", line):
            if field.startswith("start="):
                start = field.split("=", 1)[1]
            elif field.startswith("size="):
                size = field.split("=", 1)[1]
            elif field.startswith("type="):
                ptype = field.split("=", 1)[1]
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


def flush(device: str, partitions: List[str]) -> None:
    """Flush tous les buffers avant de démonter kpartx (évite les données fantômes)."""
    run(["sync"])
    for p in partitions:
        run(["blockdev", "--flushbufs", p], check=False)
    run(["blockdev", "--flushbufs", device], check=False)
    run(["sync"])
