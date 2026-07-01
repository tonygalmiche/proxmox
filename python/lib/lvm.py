"""
lvm.py — Opérations LVM : activation/désactivation de VG, création de PV/VG/LV.

Pour --init, recrée proprement le VG sur la partition destination (cleanup
complet des device-mapper avant pvcreate pour éviter les conflits udev).
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from run import run, ssh


@dataclass
class LvInfo:
    name: str
    size_bytes: int
    path: str


def get_remote_vg(host: str, partition: str) -> Optional[str]:
    r = ssh(host, f"pvs -o vg_name --noheadings '{partition}' 2>/dev/null | tr -d ' '",
            capture=True, check=False)
    vg = r.stdout.strip()
    return vg or None


def remote_activate(host: str, partition: str, vg: str) -> None:
    r = ssh(host,
            f"pvscan --cache '{partition}' 2>/dev/null; "
            f"vgchange -ay '{vg}'",
            capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"Impossible d'activer le VG '{vg}' sur {host}:\n{r.stdout}\n{r.stderr}"
        )
    active = sum(1 for line in r.stdout.splitlines()
                 if "logical volume(s) in volume group" in line and not line.strip().startswith("0 "))
    if active == 0:
        raise RuntimeError(
            f"VG '{vg}' activé mais aucun LV actif sur {host}:\n{r.stdout}"
        )


def remote_deactivate(host: str, vg: str) -> None:
    ssh(host, f"vgchange -an '{vg}' 2>/dev/null", check=False)


def get_remote_lvs(host: str, vg: str) -> List[LvInfo]:
    r = ssh(host,
            f"lvs --noheadings -o lv_name,lv_size,lv_path --units b '{vg}' 2>/dev/null",
            capture=True)
    lvs = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            lvs.append(LvInfo(
                name=parts[0],
                size_bytes=int(parts[1].rstrip("B")),
                path=parts[2],
            ))
    return lvs


def init_vg(vg: str, partition: str) -> None:
    """Nettoie tout résidu LVM sur partition, puis recrée PV + VG proprement."""
    run(["vgchange", "-an", vg], check=False)

    # Supprime les device-mapper du VG (udev peut les avoir auto-activés)
    dm_prefix = vg.replace("-", "--")
    r = run(["dmsetup", "ls", "--noheadings"], capture=True, check=False)
    for line in r.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if re.match(f"^{re.escape(dm_prefix)}-", name):
            run(["dmsetup", "remove", "--force", name], check=False)

    run(["vgremove", "-f", vg], check=False)
    run(["wipefs", "-a", partition], check=False)
    run(["pvscan", "--cache", partition], check=False)

    r = run(["pvcreate", "-ff", "-y", partition], capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"pvcreate échoué sur {partition}:\n{r.stderr}")

    r = run(["vgcreate", vg, partition], capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"vgcreate {vg} {partition} échoué:\n{r.stderr}")


def create_lv(vg: str, name: str, size_bytes: int) -> None:
    run(["lvcreate", "-L", f"{size_bytes}B", "-n", name, vg], check=False)


def activate(vg: str) -> None:
    run(["vgchange", "-ay", vg], check=False)


def deactivate(vg: str) -> None:
    run(["vgchange", "-an", vg], check=False)
