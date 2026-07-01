"""
cleanup.py — Nettoyage garanti à la fin du script (succès ou interruption).

Enregistré via atexit + SIGTERM/SIGINT. Le script principal met à jour les
attributs au fur et à mesure qu'il alloue des ressources.
"""
import atexit
import signal
from typing import List, Optional

from run import run, ssh_script


_REMOTE_CLEANUP = r"""
NBD="$1" BASE="$2"
for m in $(mount | awk -v b="$BASE" 'index($3,b)==1{print $3}'); do
    umount "$m" 2>/dev/null || true
done
vgchange -an 2>/dev/null || true
kpartx -d "$NBD" >/dev/null 2>&1 || true
qemu-nbd --disconnect "$NBD" >/dev/null 2>&1 || true
"""


class Cleanup:
    def __init__(self, host: str, nbd_device: str, grub_nbd: str, src_mount_base: str):
        self.host = host
        self.nbd_device = nbd_device
        self.grub_nbd = grub_nbd
        self.src_mount_base = src_mount_base

        self.dst_mounts: List[str] = []
        self.pve_dev: Optional[str] = None
        self.lvm_vg: Optional[str] = None
        self.grub_mnt: Optional[str] = None

        atexit.register(self.run)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, signum, frame):
        raise SystemExit(1)

    def run(self) -> None:
        import glob

        if self.grub_mnt:
            for fs in ("sys", "proc"):
                run(["umount", f"{self.grub_mnt}/{fs}"], check=False)
            for node in [self.grub_nbd] + sorted(glob.glob(f"{self.grub_nbd}p*")):
                target = f"{self.grub_mnt}{node}"
                if _is_mount(target):
                    run(["umount", target], check=False)
            run(["umount", f"{self.grub_mnt}/dev"], check=False)
            for _ in range(2):
                run(["umount", self.grub_mnt], check=False)
            self.grub_mnt = None

        for mnt in reversed(self.dst_mounts):
            run(["umount", mnt], check=False)
        self.dst_mounts.clear()

        if self.lvm_vg:
            run(["vgchange", "-an", self.lvm_vg], check=False)
            self.lvm_vg = None

        if self.pve_dev:
            run(["kpartx", "-d", self.pve_dev], check=False)
            self.pve_dev = None

        run(["qemu-nbd", "--disconnect", self.grub_nbd], check=False)

        ssh_script(self.host, _REMOTE_CLEANUP,
                   self.nbd_device, self.src_mount_base, check=False)


def _is_mount(path: str) -> bool:
    try:
        import subprocess
        return subprocess.run(["mountpoint", "-q", path], check=False).returncode == 0
    except Exception:
        return False
