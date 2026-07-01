"""
filesystem.py — Création de filesystems, montage, vérification e2fsck.

mkfs() recrée un filesystem en conservant l'UUID source (cohérence fstab/GRUB).
Pour ext4, désactive les features incompatibles avec l'outil GRUB de Proxmox
(metadata_csum, 64bit) et celles qui bloquent le boot (orphan_file).
"""
import os
from typing import Optional

from run import run, ssh


def get_remote_info(host: str, partition: str) -> dict:
    """Retourne fstype, uuid, label via blkid."""
    info = {}
    for key in ("TYPE", "UUID", "LABEL"):
        r = ssh(host, f"blkid -o value -s {key} '{partition}'",
                capture=True, check=False)
        info[key.lower()] = r.stdout.strip()
    return info


def mkfs(fstype: str, device: str, uuid: Optional[str] = None,
         label: Optional[str] = None) -> None:
    if fstype in ("ext2", "ext3"):
        cmd = ["mkfs." + fstype, "-q", "-F"]
        if uuid:
            cmd += ["-U", uuid]
        run(cmd + [device])

    elif fstype == "ext4":
        cmd = ["mkfs.ext4", "-q", "-F",
               "-O", "^metadata_csum,^metadata_csum_seed,^64bit,^orphan_file"]
        if uuid:
            cmd += ["-U", uuid]
        run(cmd + [device])
        # tune2fs en renfort : certaines versions d'e2fsprogs ignorent ^orphan_file à mkfs
        run(["tune2fs", "-O", "^orphan_file", device], check=False)

    elif fstype == "xfs":
        run(["mkfs.xfs", "-q", "-f", device])
        if uuid:
            run(["xfs_admin", "-U", uuid, device], check=False)

    elif fstype == "vfat":
        cmd = ["mkfs.vfat", "-F32"]
        if uuid:
            cmd += ["-i", uuid.replace("-", "")]
        if label:
            cmd += ["-n", label]
        run(cmd + [device])

    elif fstype == "swap":
        cmd = ["mkswap"]
        if uuid:
            cmd += ["-U", uuid]
        run(cmd + [device])

    else:
        raise ValueError(f"Filesystem '{fstype}' non géré.")


def e2fsck(device: str) -> None:
    """Vérifie/répare le filesystem ext* avant montage. Code ≥ 4 = erreur fatale."""
    r = run(["e2fsck", "-fy", device], capture=True, check=False)
    if r.returncode >= 4:
        raise RuntimeError(
            f"e2fsck ne peut pas réparer {device} (code {r.returncode}):\n{r.stdout}"
        )


def mount_local(device: str, mountpoint: str, options: Optional[str] = None) -> None:
    os.makedirs(mountpoint, exist_ok=True)
    cmd = ["mount"]
    if options:
        cmd += ["-o", options]
    run(cmd + [device, mountpoint])


def mount_remote(host: str, device: str, mountpoint: str) -> None:
    ssh(host, f"umount '{mountpoint}' 2>/dev/null; mkdir -p '{mountpoint}' "
              f"&& mount -o ro '{device}' '{mountpoint}'")


def umount_local(mountpoint: str) -> None:
    run(["umount", mountpoint], check=False)


def umount_remote(host: str, mountpoint: str) -> None:
    ssh(host, f"umount '{mountpoint}'", check=False)
