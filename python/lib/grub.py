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
import time
from typing import Dict, Optional, Tuple

import nbd as nbd_mod
import partition as part
import proxmox as pve
from filesystem import mount_local
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


def reinstall_bios_boot(vmid: str, pve_dev: str, grub_nbd: str, dst_mount_base: str) -> None:
    """Répare un boot BIOS cassé : ajoute une partition BIOS Boot (ef02) si
    le disque est en GPT sans espace d'embedding, puis réinstalle GRUB.
    Démarre/vérifie la VM si elle tournait avant l'appel."""
    was_running = pve.get_status(vmid) == "running"
    if was_running:
        print(f"  Arrêt de la VM {vmid}...")
        pve.stop_vm(vmid)

    gap = part.find_bios_boot_gap(pve_dev)
    if gap:
        print(f"  Ajout d'une partition BIOS Boot (ef02) sur {pve_dev} "
              f"[secteurs {gap[0]}:{gap[1]}]")
        part.add_bios_boot_partition(pve_dev, gap[0], gap[1])
    else:
        print(f"  Aucune partition BIOS Boot à ajouter sur {pve_dev} "
              f"(déjà présente, disque non-GPT, ou pas assez de place).")

    nbd_mod.local_connect(grub_nbd, pve_dev)
    try:
        grub_root_dev, root_uuid = _find_root_partition(grub_nbd)
        if not grub_root_dev:
            raise RuntimeError(
                f"Partition racine introuvable sur {pve_dev} "
                f"(aucune partition avec /etc/fstab)."
            )

        grub_mnt = f"{dst_mount_base}/grub-fix-{vmid}"
        mount_local(grub_root_dev, grub_mnt)
        print(f"  Réinstallation de GRUB sur {grub_root_dev}...")
        install_bios(grub_mnt, grub_nbd, root_uuid)
    finally:
        nbd_mod.local_disconnect(grub_nbd)

    if was_running:
        print(f"  Démarrage de la VM {vmid}...")
        pve.start_vm(vmid)
        _verify_boot(vmid)
    else:
        print(f"  VM {vmid} laissée arrêtée (elle était arrêtée avant la réparation).")


def _find_root_partition(nbd_device: str) -> Tuple[Optional[str], Optional[str]]:
    """Trouve, parmi les partitions du device NBD, celle qui contient /etc/fstab.
    Retourne (device, uuid) ou (None, None)."""
    probe_mnt = "/mnt/_grubfix_probe"
    os.makedirs(probe_mnt, exist_ok=True)

    for part_dev in sorted(glob.glob(f"{nbd_device}p*")):
        fstype = run(["blkid", "-o", "value", "-s", "TYPE", part_dev],
                     capture=True, check=False).stdout.strip()
        if fstype not in ("ext2", "ext3", "ext4", "xfs"):
            continue

        run(["mount", "-o", "ro", part_dev, probe_mnt], check=False)
        is_root = os.path.exists(os.path.join(probe_mnt, "etc/fstab"))
        run(["umount", probe_mnt], check=False)

        if is_root:
            uuid = run(["blkid", "-o", "value", "-s", "UUID", part_dev],
                      capture=True).stdout.strip()
            return part_dev, uuid

    return None, None


def _verify_boot(vmid: str, timeout: int = 30, interval: int = 5) -> None:
    """Surveille cpu/mem de la VM après démarrage pour détecter une boucle
    de boot cassé (CPU bloqué à ~100%, RAM qui ne monte jamais)."""
    print(f"  Vérification du boot (jusqu'à {timeout}s)...")
    start = time.time()
    samples = []
    while time.time() - start < timeout:
        time.sleep(interval)
        status = pve.get_verbose_status(vmid)
        try:
            cpu = float(status.get("cpu", 0))
            mem = int(status.get("mem", 0))
        except ValueError:
            continue
        samples.append((cpu, mem))
        print(f"    t+{int(time.time() - start)}s : "
              f"cpu={cpu * 100:.1f}%  mem={mem // (1024 * 1024)}MiB")

    if not samples:
        print("  Impossible de vérifier l'état de la VM (pas de données).")
        return

    last_cpu, last_mem = samples[-1]
    if last_cpu > 0.8 and last_mem < 200 * 1024 * 1024:
        print("  ⚠️  La VM semble toujours bloquée au boot "
              "(CPU élevé, peu de RAM utilisée). Vérifiez la console.")
    else:
        print("  ✅ La VM semble démarrer normalement "
              "(CPU redescendu et/ou RAM en hausse).")


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
