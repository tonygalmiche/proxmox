#!/usr/bin/env python3
"""
migrer-vm-opennebula-vers-proxmox.py — Migration d'une VM d'OpenNebula vers Proxmox.

Usage:
  ./migrer-vm-opennebula-vers-proxmox.py <nom-vm> [--create] [--init] [--rsync]

  --create  Crée la VM sur Proxmox (CPU/RAM/disques vides) d'après la config OpenNebula.
  --init    Premier passage : recrée la table de partitions + filesystems + rsync + GRUB.
  --rsync   Passages suivants : rsync uniquement (partitions déjà créées).

Les options sont cumulables : --create --init lance les deux en séquence.
"""
import argparse
import math
import os
import re
import sys

# Ajoute lib/ au path Python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import config as cfg_mod
import cleanup as cleanup_mod
import filesystem as fs
import grub as grub_mod
import lvm
import nbd as nbd_mod
import opennebula as on_mod
import partition as part
import proxmox as pve
import sync as sync_mod

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.ini")
REQUIRED_TOOLS = [
    "python3", "pvesm", "qm", "qemu-img", "qemu-nbd",
    "sfdisk", "partprobe", "kpartx", "blkid", "blockdev",
    "rsync", "chroot", "mknod", "tune2fs", "e2fsck",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    print(f"Erreur : {msg}", file=sys.stderr)
    sys.exit(1)


def check_root() -> None:
    if os.getuid() != 0:
        die("ce script doit être exécuté en root (mount/mkfs/chroot).")


def check_tools() -> None:
    missing = [t for t in REQUIRED_TOOLS if not _which(t)]
    if missing:
        die(f"commandes manquantes : {', '.join(missing)}")


def _which(cmd: str) -> bool:
    return any(
        os.path.isfile(os.path.join(d, cmd))
        for d in os.environ.get("PATH", "").split(":")
    )


def mb_to_gb_ceil(mb: int) -> int:
    return math.ceil(mb / 1024)


# ---------------------------------------------------------------------------
# --create : crée la VM Proxmox vide d'après la config OpenNebula
# ---------------------------------------------------------------------------

def create_vm(vm_name: str, cfg) -> None:
    on_vm = on_mod.find_vm(cfg.opennebula_host, vm_name)

    existing = pve.find_vm(vm_name)
    if existing:
        print(f"VM '{vm_name}' existe déjà sur Proxmox (VMID={existing.vmid}). Rien à faire.")
        return

    if not on_vm.is_stopped():
        die(f"la VM '{on_vm.name}' n'est pas arrêtée sur OpenNebula (état={on_vm.state}).")

    vmid = pve.next_vmid()
    pve.create_vm(vmid, vm_name, on_vm.memory_mb, on_vm.vcpu, cfg.proxmox_bridge)

    print(f"{'VM':<30} {'ON_ID':<8} {'VCPU':<6} {'RAM(Mo)':<10} "
          f"{'VMID':<8} {'DISK':<6} {'IMAGE':<30} {'ON(Mo)':<10} {'PVE(Go)'}")
    for disk in on_vm.disks:
        size_gb = mb_to_gb_ceil(disk.size_mb)
        pve.add_disk(vmid, disk.disk_id, cfg.proxmox_storage, size_gb)
        print(f"{vm_name:<30} {on_vm.vm_id:<8} {on_vm.vcpu:<6} {on_vm.memory_mb:<10} "
              f"{vmid:<8} {disk.disk_id:<6} {disk.image:<30} {disk.size_mb:<10} {size_gb}")

    pve.set_boot_disk(vmid)
    pve.set_serial(vmid)
    print(f"VM '{vm_name}' créée sur Proxmox (VMID={vmid}).")


# ---------------------------------------------------------------------------
# --init / --rsync : synchronisation des disques
# ---------------------------------------------------------------------------

def sync_vm(vm_name: str, cfg, init: bool) -> None:
    on_vm = on_mod.find_vm(cfg.opennebula_host, vm_name)
    if not on_vm.is_stopped():
        die(f"la VM '{on_vm.name}' n'est pas arrêtée sur OpenNebula (état={on_vm.state}).")

    pve_vm = pve.find_vm(vm_name)
    if not pve_vm:
        die(f"VM Proxmox '{vm_name}' introuvable. Lancez d'abord --create.")
    if pve.get_status(pve_vm.vmid) != "stopped":
        die(f"la VM Proxmox '{vm_name}' (VMID={pve_vm.vmid}) n'est pas arrêtée.")

    pve_disks = pve.get_disks(pve_vm.vmid)
    if len(pve_disks) != len(on_vm.disks):
        die(f"nombre de disques différent : OpenNebula={len(on_vm.disks)}, "
            f"Proxmox={len(pve_disks)}.")

    import time
    start = time.time()
    print(f"VM '{vm_name}' : OpenNebula ID={on_vm.vm_id} ↔ Proxmox VMID={pve_vm.vmid} "
          f"({len(on_vm.disks)} disque(s), init={'oui' if init else 'non'}) "
          f"— début {time.strftime('%H:%M:%S')}")

    cleanup = cleanup_mod.Cleanup(
        host=cfg.opennebula_host,
        nbd_device=cfg.nbd_device,
        grub_nbd=cfg.grub_nbd_device,
        src_mount_base=cfg.src_mount_base,
    )

    for on_disk, pve_disk in zip(on_vm.disks, pve_disks):
        _migrate_disk(on_disk.disk_id, on_disk.source,
                      pve_disk.volume, pve_vm.vmid, cfg, init, cleanup)

    elapsed = int(time.time() - start)
    print(f"Synchronisation terminée pour '{vm_name}' (VMID={pve_vm.vmid}) "
          f"— fin {time.strftime('%H:%M:%S')} (durée {elapsed}s)")


def _migrate_disk(disk_id: int, on_source: str, pve_volume: str,
                  proxmox_vmid: str, cfg, init: bool,
                  cleanup: cleanup_mod.Cleanup) -> None:

    pve_dev = pve.get_disk_path(pve_volume)
    cleanup.pve_dev = pve_dev

    # 1. Connexion NBD source
    sfdisk_dump, src_parts = nbd_mod.remote_connect(
        cfg.opennebula_host, on_source, cfg.nbd_device, cfg.src_mount_base
    )

    # 2. Table de partitions côté Proxmox (--init uniquement)
    if init and sfdisk_dump.strip():
        print(f"  Copie de la table de partitions → {pve_dev}")
        part.apply_table(pve_dev, sfdisk_dump)

    dst_parts = part.kpartx_add(pve_dev)

    # 3. Vérification de cohérence
    _check_partition_consistency(cfg.opennebula_host, cfg.nbd_device,
                                 sfdisk_dump, src_parts,
                                 pve_dev, dst_parts)

    # 4. Synchronisation partition par partition
    root_dst_mount = None
    root_part_num = None
    root_fsuuid = None
    grub_has_esp = False

    os.makedirs(cfg.src_mount_base, exist_ok=True)
    os.makedirs(cfg.dst_mount_base, exist_ok=True)

    for idx, (src_part, dst_part) in enumerate(zip(src_parts, dst_parts)):
        info = fs.get_remote_info(cfg.opennebula_host, src_part)
        fstype = info["type"]
        fsuuid = info["uuid"]

        if fstype == "LVM2_member":
            result = _handle_lvm(disk_id, idx, src_part, dst_part,
                                 cfg, init, cleanup)
            if result:
                root_dst_mount, root_fsuuid = result
            continue

        if fstype == "vfat":
            label = info["label"]
            if label == "ESP":
                grub_has_esp = True

        if fstype == "swap":
            if init:
                fs.mkfs("swap", dst_part, uuid=fsuuid)
            continue

        if not fstype:
            continue

        if init:
            label = info.get("label") if fstype == "vfat" else None
            fs.mkfs(fstype, dst_part, uuid=fsuuid, label=label)

        if fstype in ("ext2", "ext3", "ext4"):
            fs.e2fsck(dst_part)

        src_mnt = f"{cfg.src_mount_base}/disk{disk_id}-p{idx}"
        dst_mnt = f"{cfg.dst_mount_base}/disk{disk_id}-p{idx}"

        fs.mount_remote(cfg.opennebula_host, src_part, src_mnt)
        mount_opts = "umask=0022" if fstype == "vfat" else None
        fs.mount_local(dst_part, dst_mnt, options=mount_opts)
        cleanup.dst_mounts.append(dst_mnt)

        sync_mod.rsync(cfg.opennebula_host, src_mnt, dst_mnt, is_vfat=(fstype == "vfat"))

        fs.umount_remote(cfg.opennebula_host, src_mnt)

        if os.path.exists(os.path.join(dst_mnt, "etc/fstab")):
            root_dst_mount = dst_mnt
            root_part_num = re.search(r"\d+$", dst_part)
            root_part_num = root_part_num.group() if root_part_num else None
            root_fsuuid = fsuuid

    # 5. Démontage + flush avant de retirer kpartx
    for mnt in cleanup.dst_mounts:
        fs.umount_local(mnt)
    cleanup.dst_mounts.clear()

    part.flush(pve_dev, dst_parts)

    if cleanup.lvm_vg:
        lvm.deactivate(cleanup.lvm_vg)
        cleanup.lvm_vg = None

    part.kpartx_remove(pve_dev)
    cleanup.pve_dev = None

    # 6. Post-traitement : UEFI ou GRUB BIOS
    _post_sync(disk_id, pve_dev, proxmox_vmid, cfg, cleanup,
               root_dst_mount, root_part_num, root_fsuuid, grub_has_esp)

    nbd_mod.remote_disconnect(cfg.opennebula_host, cfg.nbd_device, cfg.src_mount_base)


def _handle_lvm(disk_id: int, part_idx: int, src_part: str, dst_part: str,
                cfg, init: bool,
                cleanup: cleanup_mod.Cleanup):
    """Gère une partition LVM2_member : active le VG source, recrée ou active le VG dest."""
    host = cfg.opennebula_host

    vg = lvm.get_remote_vg(host, src_part)
    if not vg:
        print(f"  Attention : LVM2_member sur {src_part} sans VG connu, ignoré.",
              file=sys.stderr)
        return None

    lvm.remote_activate(host, src_part, vg)
    lvs = lvm.get_remote_lvs(host, vg)

    if init:
        lvm.init_vg(vg, dst_part)
        for lv in lvs:
            lvm.create_lv(vg, lv.name, lv.size_bytes)

    lvm.activate(vg)
    cleanup.lvm_vg = vg

    root_dst_mount = None
    root_fsuuid = None

    for lv in lvs:
        src_lv = lv.path
        dst_lv = f"/dev/{vg}/{lv.name}"

        info = fs.get_remote_info(host, src_lv)
        lft = info["type"]
        lfu = info["uuid"]

        if lft == "swap":
            if init:
                fs.mkfs("swap", dst_lv, uuid=lfu)
            continue
        if not lft:
            continue

        if init:
            fs.mkfs(lft, dst_lv, uuid=lfu)

        if lft in ("ext2", "ext3", "ext4"):
            fs.e2fsck(dst_lv)

        src_mnt = f"{cfg.src_mount_base}/disk{disk_id}-lv{lv.name}"
        dst_mnt = f"{cfg.dst_mount_base}/disk{disk_id}-lv{lv.name}"

        fs.mount_remote(host, src_lv, src_mnt)
        fs.mount_local(dst_lv, dst_mnt)
        cleanup.dst_mounts.append(dst_mnt)

        sync_mod.rsync(host, src_mnt, dst_mnt)
        fs.umount_remote(host, src_mnt)

        if os.path.exists(os.path.join(dst_mnt, "etc/fstab")):
            root_dst_mount = dst_mnt
            root_fsuuid = lfu
            grub_mod.apply_root_fixes(dst_mnt, cfg.proxmox_net_iface)

    lvm.remote_deactivate(host, vg)
    return (root_dst_mount, root_fsuuid) if root_dst_mount else None


def _post_sync(disk_id: int, pve_dev: str, proxmox_vmid: str, cfg,
               cleanup: cleanup_mod.Cleanup,
               root_dst_mount, root_part_num, root_fsuuid, grub_has_esp: bool) -> None:

    if not root_dst_mount:
        return

    if grub_has_esp:
        # VM UEFI : rsync a déjà copié les fichiers EFI — configure juste Proxmox
        pve.set_uefi(proxmox_vmid, cfg.proxmox_storage)
        pve.set_serial(proxmox_vmid)

    elif root_part_num:
        # VM BIOS/MBR : installe GRUB via qemu-nbd local
        import run as _run_mod
        _run_mod.run(["sync"])
        _run_mod.run(["blockdev", "--flushbufs", pve_dev], check=False)

        nbd_mod.local_connect(cfg.grub_nbd_device, pve_dev)
        grub_root_dev = f"{cfg.grub_nbd_device}p{root_part_num}"
        grub_mnt = f"{cfg.dst_mount_base}/grub-disk{disk_id}"

        fs.mount_local(grub_root_dev, grub_mnt)
        cleanup.grub_mnt = grub_mnt

        if not os.path.exists(os.path.join(grub_mnt, "etc/fstab")):
            print(f"  Attention : {grub_root_dev} ne contient pas /etc/fstab, GRUB ignoré.",
                  file=sys.stderr)
        else:
            grub_mod.apply_root_fixes(grub_mnt, cfg.proxmox_net_iface)
            grub_mod.install_bios(grub_mnt, cfg.grub_nbd_device, root_fsuuid)

        cleanup.grub_mnt = None
        nbd_mod.local_disconnect(cfg.grub_nbd_device)
        pve.set_serial(proxmox_vmid)

    else:
        # VM BIOS + LVM : GRUB non automatisé
        print("  Attention : GRUB sur BIOS+LVM non géré. Réinstallez manuellement.",
              file=sys.stderr)
        pve.set_serial(proxmox_vmid)


def _check_partition_consistency(host: str, nbd_device: str,
                                 sfdisk_dump: str, src_parts: list,
                                 pve_dev: str, dst_parts: list) -> None:
    if len(src_parts) != len(dst_parts):
        raise RuntimeError(
            f"Nombre de partitions différent : source={len(src_parts)}, "
            f"destination={len(dst_parts)}."
        )

    has_table_src = len(src_parts) > 1 or src_parts[0] != nbd_device
    has_table_dst = dst_parts[0] != pve_dev

    if has_table_src != has_table_dst:
        raise RuntimeError("Présence d'une table de partitions différente entre source et destination.")

    if has_table_src:
        dst_dump = part.get_local_dump(pve_dev)
        if part.normalize(sfdisk_dump) != part.normalize(dst_dump):
            raise RuntimeError(
                f"Table de partitions source ≠ destination.\n"
                f"  Source      : {part.normalize(sfdisk_dump)}\n"
                f"  Destination : {part.normalize(dst_dump)}\n"
                f"Relancez avec --init pour la recréer."
            )
    else:
        src_size = part.get_remote_size(host, nbd_device)
        dst_size = part.get_size(pve_dev)
        if src_size != dst_size:
            raise RuntimeError(
                f"Taille de disque différente : source={src_size}, destination={dst_size}."
            )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migration VM OpenNebula → Proxmox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("vm_name", help="Nom de la VM")
    parser.add_argument("--create", action="store_true",
                        help="Crée la VM sur Proxmox (disques vides)")
    parser.add_argument("--init", action="store_true",
                        help="Premier passage : recrée partitions + filesystems + rsync + GRUB")
    parser.add_argument("--rsync", action="store_true",
                        help="Passages suivants : rsync uniquement")
    args = parser.parse_args()

    if not (args.create or args.init or args.rsync):
        parser.error("Au moins une option parmi --create, --init, --rsync est requise.")

    check_root()
    check_tools()

    cfg = cfg_mod.load(CONFIG_PATH)

    if args.create:
        create_vm(args.vm_name, cfg)

    if args.init or args.rsync:
        if args.init:
            print(f"⚠️  --init va recréer les tables de partitions et filesystems "
                  f"sur les disques Proxmox de '{args.vm_name}'.")
            answer = input("Confirmer ? (oui/non) : ").strip()
            if answer != "oui":
                print("Annulé.")
                sys.exit(0)
        sync_vm(args.vm_name, cfg, init=args.init)


if __name__ == "__main__":
    main()
