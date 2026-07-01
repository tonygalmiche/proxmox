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


def remote_get_vg_backup(host: str, vg: str) -> str:
    """Retourne le contenu du backup vgcfgbackup du VG source (préserve VG+LV UUIDs)."""
    r = ssh(host,
            f"vgcfgbackup -f /tmp/_vgcfg_{vg} '{vg}' >/dev/null 2>&1 "
            f"&& cat /tmp/_vgcfg_{vg} && rm -f /tmp/_vgcfg_{vg}",
            capture=True, check=False)
    return r.stdout


def _parse_pv_uuid(backup: str) -> Optional[str]:
    m = re.search(
        r'physical_volumes\s*\{.*?id\s*=\s*"'
        r'([A-Za-z0-9]{6}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}'
        r'-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{6})"',
        backup, re.DOTALL,
    )
    return m.group(1) if m else None


def init_vg(vg: str, partition: str, vg_backup: Optional[str] = None) -> bool:
    """Nettoie tout résidu LVM sur partition, puis recrée le VG.

    Si vg_backup (contenu de vgcfgbackup) est fourni, utilise pvcreate --restorefile
    + vgcfgrestore pour préserver VG UUID, LV UUIDs et les paths dm-uuid dans fstab/grub.

    Retourne True si la structure LV a été restaurée (pas besoin de lvcreate),
    False si le VG a été recréé vide (lvcreate nécessaire).
    """
    run(["vgchange", "-an", vg], check=False)

    dm_prefix = vg.replace("-", "--")
    r = run(["dmsetup", "ls", "--noheadings"], capture=True, check=False)
    for line in r.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if re.match(f"^{re.escape(dm_prefix)}-", name):
            run(["dmsetup", "remove", "--force", name], check=False)

    run(["vgremove", "-f", vg], check=False)
    run(["wipefs", "-a", partition], check=False)
    run(["pvscan", "--cache", partition], check=False)

    if vg_backup:
        import tempfile, os as _os
        with tempfile.NamedTemporaryFile("w", suffix=".bak", delete=False) as tmp:
            tmp.write(vg_backup)
            backup_file = tmp.name
        try:
            pv_uuid = _parse_pv_uuid(vg_backup)
            pvcreate_cmd = ["pvcreate", "-ff", "-y"]
            if pv_uuid:
                pvcreate_cmd += ["--uuid", pv_uuid, "--restorefile", backup_file]
            pvcreate_cmd.append(partition)
            r = run(pvcreate_cmd, capture=True, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"pvcreate échoué sur {partition}:\n{r.stderr}")

            r = run(["vgcfgrestore", "-f", backup_file, vg], capture=True, check=False)
            if r.returncode == 0:
                return True
            print(f"  Attention : vgcfgrestore échoué, recréation sans préservation UUID:\n{r.stderr}",
                  file=__import__("sys").stderr)
        finally:
            _os.unlink(backup_file)

    # Fallback : vgcreate standard
    r = run(["pvcreate", "-ff", "-y", partition], capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"pvcreate échoué sur {partition}:\n{r.stderr}")
    r = run(["vgcreate", vg, partition], capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"vgcreate {vg} {partition} échoué:\n{r.stderr}")
    return False


def create_lv(vg: str, name: str, size_bytes: int) -> None:
    run(["lvcreate", "-L", f"{size_bytes}B", "-n", name, vg], check=False)


def activate(vg: str) -> None:
    run(["vgchange", "-ay", vg], check=False)


def deactivate(vg: str) -> None:
    run(["vgchange", "-an", vg], check=False)
