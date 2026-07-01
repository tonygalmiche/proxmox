"""
grub.py — Réinstallation de GRUB BIOS dans un chroot isolé + corrections post-migration.

apply_root_fixes() : renomme l'interface réseau et active la console série ttyS0.
install_bios()     : monte un /dev minimal isolé, exécute grub-install + update-grub
                     dans un chroot, puis corrige grub.cfg si update-grub a écrit
                     le chemin NBD temporaire au lieu de l'UUID.

Le /dev isolé évite que GRUB y voie à la fois le device NBD et le volume LVM
d'origine (même octets, deux chemins), ce qui déclencherait "unknown filesystem".
"""
import glob
import os
import re

from run import run


def apply_root_fixes(root_mnt: str, net_iface: str) -> None:
    """Renomme l'interface réseau et active la console série."""
    ifaces_file = os.path.join(root_mnt, "etc/network/interfaces")
    if os.path.exists(ifaces_file):
        with open(ifaces_file) as f:
            content = f.read()
        old_ifaces = {
            parts[1]
            for line in content.splitlines()
            if (parts := line.split()) and len(parts) > 1
            and parts[0] in ("auto", "iface", "allow-hotplug")
            and parts[1] != "lo"
        }
        for old in old_ifaces:
            if old != net_iface:
                content = re.sub(rf"\b{re.escape(old)}\b", net_iface, content)
        with open(ifaces_file, "w") as f:
            f.write(content)

    grub_default = os.path.join(root_mnt, "etc/default/grub")
    if os.path.exists(grub_default):
        with open(grub_default) as f:
            content = f.read()
        if "console=ttyS0" not in content:
            content = re.sub(
                r'^(GRUB_CMDLINE_LINUX_DEFAULT=")(.*)"',
                r'\1\2 console=tty0 console=ttyS0,115200n8"',
                content, flags=re.MULTILINE,
            )
            with open(grub_default, "w") as f:
                f.write(content)

    getty_dir = os.path.join(root_mnt, "etc/systemd/system/getty.target.wants")
    os.makedirs(getty_dir, exist_ok=True)
    link = os.path.join(getty_dir, "serial-getty@ttyS0.service")
    if not os.path.lexists(link):
        try:
            os.symlink("/lib/systemd/system/serial-getty@.service", link)
        except OSError:
            pass


def install_bios(grub_mnt: str, grub_nbd: str, root_uuid: str) -> None:
    """Installe GRUB BIOS dans un chroot avec /dev minimal isolé."""
    run(["mount", "--bind", grub_mnt, grub_mnt])

    run(["mount", "-t", "tmpfs", "-o", "size=1M,mode=755", "tmpfs", f"{grub_mnt}/dev"])
    for name, major, minor, mode in [
        ("null", 1, 3, "666"), ("zero", 1, 5, "666"),
        ("random", 1, 8, "444"), ("urandom", 1, 9, "444"),
        ("console", 5, 1, "600"),
    ]:
        run(["mknod", "-m", mode, f"{grub_mnt}/dev/{name}", "c", str(major), str(minor)])

    for node in [grub_nbd] + sorted(glob.glob(f"{grub_nbd}p*")):
        if not os.path.exists(node):
            continue
        target = f"{grub_mnt}{node}"
        open(target, "a").close()
        run(["mount", "--bind", node, target])

    for fs in ("proc", "sys"):
        run(["mount", "--bind", f"/{fs}", f"{grub_mnt}/{fs}"])

    for cmd in (
        ["chroot", grub_mnt, "grub-install", "--target=i386-pc", grub_nbd],
        ["chroot", grub_mnt, "update-grub"],
    ):
        r = run(cmd, capture=True, check=False)
        if r.returncode != 0:
            print(f"  Attention : {cmd[2]} échoué:\n{r.stdout}{r.stderr}")

    # Corrige grub.cfg si update-grub a écrit le chemin NBD temporaire
    grub_cfg = os.path.join(grub_mnt, "boot/grub/grub.cfg")
    if root_uuid and os.path.exists(grub_cfg):
        with open(grub_cfg) as f:
            cfg = f.read()
        cfg = re.sub(r"root=/dev/nbd\d+p\d+", f"root=UUID={root_uuid}", cfg)
        with open(grub_cfg, "w") as f:
            f.write(cfg)

    _teardown(grub_mnt, grub_nbd)


def _teardown(grub_mnt: str, grub_nbd: str) -> None:
    for fs in ("sys", "proc"):
        run(["umount", f"{grub_mnt}/{fs}"], check=False)
    for node in [grub_nbd] + sorted(glob.glob(f"{grub_nbd}p*")):
        target = f"{grub_mnt}{node}"
        if os.path.ismount(target):
            run(["umount", target], check=False)
    run(["umount", f"{grub_mnt}/dev"], check=False)
    run(["umount", grub_mnt], check=False)
    run(["umount", grub_mnt], check=False)  # double : bind-mount de la racine sur elle-même
