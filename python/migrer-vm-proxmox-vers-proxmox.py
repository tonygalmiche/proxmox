#!/usr/bin/env python3
"""
migrer-vm-proxmox-vers-proxmox.py — Synchronise les disques d'une VM depuis un serveur
Proxmox source vers le même VM (déjà créée, disques vides) sur un serveur Proxmox
destination, en rsync direct via SSH — sans stockage intermédiaire (pas de PBS, pas
de NBD : la source est déjà un volume LVM/LVM-thin exposé directement via kpartx).

Usage:
  ./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --create
  ./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --init
  ./migrer-vm-proxmox-vers-proxmox.py <source_host> <vm_name> --rsync

  --create  Crée la VM sur ce serveur (CPU/RAM/disques vides) d'après la
            config de la VM source. Ne fait rien si la VM existe déjà ici.
  --init    Premier passage : recopie la table de partitions, recrée les
            filesystems, puis rsync complet. Destructif pour les disques
            destination (normalement vides à ce stade).
  --rsync   Passages suivants : rsync incrémental uniquement (partitions et
            filesystems déjà en place).

Les options sont cumulables : --create --init lance les deux en séquence.

Version volontairement minimale (v1) :
- --create crée la VM destination avec les mêmes slots scsi/virtio/... que la
  source (CPU/RAM copiés, disques vides de même taille) ;
- filesystems gérés : ext4, xfs, swap (les autres sont signalés et ignorés) ;
- pas de gestion GRUB/UEFI, pas de LVM interne à la VM : à ajouter si besoin
  une fois ce socle validé.
"""
import argparse
import math
import os
import re
import subprocess
import sys
import time

REQUIRED_TOOLS = [
    "qm", "pvesm", "kpartx", "sfdisk", "partprobe", "blkid",
    "mkfs.ext4", "mkfs.xfs", "xfs_admin", "mkswap", "rsync", "ssh",
]
SRC_MOUNT_BASE = "/mnt/migrate-src"
DST_MOUNT_BASE = "/mnt/migrate-dst"


# ---------------------------------------------------------------------------
# Helpers d'exécution (local ou via SSH selon host=None/host=<nom>)
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    print(f"Erreur : {msg}", file=sys.stderr)
    sys.exit(1)


def sh(host, command: str, *, check: bool = True, capture: bool = False,
       stdin: str = None) -> subprocess.CompletedProcess:
    cmd = ["ssh", host, command] if host else ["bash", "-c", command]
    r = subprocess.run(cmd, text=True, input=stdin,
                       capture_output=capture, check=False)
    if check and r.returncode != 0:
        stderr = (r.stderr or "").strip()
        raise RuntimeError(f"Échec ({'local' if not host else host}): {command}"
                           + (f"\n{stderr}" if stderr else ""))
    return r


def check_root() -> None:
    if os.getuid() != 0:
        die("ce script doit être exécuté en root (mount/mkfs).")


def check_tools() -> None:
    missing = [t for t in REQUIRED_TOOLS if not _which(t)]
    if missing:
        die(f"commandes manquantes : {', '.join(missing)}")


def _which(cmd: str) -> bool:
    return any(os.path.isfile(os.path.join(d, cmd))
               for d in os.environ.get("PATH", "").split(":"))


# ---------------------------------------------------------------------------
# Proxmox : VM et disques (local ou distant selon host)
# ---------------------------------------------------------------------------

def find_vmid(host, vm_name: str):
    r = sh(host, "qm list", capture=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == vm_name:
            return parts[0]
    return None


def get_status(host, vmid: str) -> str:
    parts = sh(host, f"qm status {vmid}", capture=True).stdout.split()
    return parts[-1] if parts else ""


def get_disks(host, vmid: str):
    """Retourne [(slot, volume, size), ...] triés (scsi0, scsi1, virtio0, ...).
    size est la valeur brute affichée par 'qm config' (ex: '32G'), ou None."""
    r = sh(host, f"qm config {vmid}", capture=True)
    disks = []
    for line in r.stdout.splitlines():
        m = re.match(r'^(scsi|virtio|ide|sata)(\d+):\s*(\S+)', line)
        if m and "media=cdrom" not in line:
            slot = m.group(1) + m.group(2)
            volume = m.group(3).split(",")[0]
            size_m = re.search(r'\bsize=(\S+)', line)
            disks.append((slot, volume, size_m.group(1) if size_m else None))
    disks.sort(key=lambda d: (re.match(r'^([a-z]+)(\d+)$', d[0]).group(1),
                               int(re.match(r'^([a-z]+)(\d+)$', d[0]).group(2))))
    return disks


def disk_path(host, volume: str) -> str:
    return sh(host, f"pvesm path {volume}", capture=True).stdout.strip()


def get_vm_specs(host, vmid: str):
    """Retourne (memory_mb, cores) lus depuis 'qm config'."""
    text = sh(host, f"qm config {vmid}", capture=True).stdout
    memory = re.search(r'^memory:\s*(\d+)', text, re.M)
    cores = re.search(r'^cores:\s*(\d+)', text, re.M)
    return (int(memory.group(1)) if memory else 512,
            int(cores.group(1)) if cores else 1)


# ---------------------------------------------------------------------------
# --create : crée la VM destination (CPU/RAM/disques vides) d'après la source
# ---------------------------------------------------------------------------

def create_vm(source_host: str, vm_name: str, storage: str, bridge: str) -> None:
    if find_vmid(None, vm_name):
        print(f"VM '{vm_name}' existe déjà sur ce serveur. Rien à faire.")
        return

    source_vmid = find_vmid(source_host, vm_name)
    if not source_vmid:
        die(f"VM '{vm_name}' introuvable sur {source_host}.")
    if get_status(source_host, source_vmid) != "stopped":
        die(f"la VM '{vm_name}' n'est pas arrêtée sur {source_host}.")

    memory_mb, cores = get_vm_specs(source_host, source_vmid)
    src_disks = get_disks(source_host, source_vmid)
    if not src_disks:
        die(f"VM '{vm_name}' sur {source_host} n'a aucun disque.")

    vmid = sh(None, "pvesh get /cluster/nextid", capture=True).stdout.strip()
    sh(None, f"qm create {vmid} --name {vm_name} --memory {memory_mb} --cores {cores} "
             f"--cpu host --ostype l26 --scsihw virtio-scsi-single "
             f"--net0 virtio,bridge={bridge},firewall=1")

    for idx, (slot, volume, size) in enumerate(src_disks):
        size_gb = math.ceil(float(size.rstrip("GgMm"))) if size else 32
        if size and size[-1] in "Mm":
            size_gb = math.ceil(size_gb / 1024)
        sh(None, f"qm set {vmid} --scsi{idx} {storage}:{size_gb},iothread=1")
        print(f"  disque {idx} : {volume} ({size or '?'}) -> {storage} ({size_gb}G, vide)")

    sh(None, f"qm set {vmid} --boot order=scsi0")
    print(f"VM '{vm_name}' créée sur ce serveur (VMID={vmid}, {memory_mb} Mo RAM, {cores} cores).")


# ---------------------------------------------------------------------------
# Partitions (kpartx local ou distant), filesystems (mkfs local uniquement)
# ---------------------------------------------------------------------------

def kpartx_add(host, device: str):
    sh(host, f"kpartx -avs {device}", check=False)
    r = sh(host, f"kpartx -l {device}", capture=True, check=False)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return [device]
    return ["/dev/mapper/" + l.split()[0] for l in lines]


def kpartx_remove(host, device: str) -> None:
    sh(host, f"kpartx -d {device}", check=False)


def sfdisk_dump(host, device: str) -> str:
    return sh(host, f"sfdisk -d {device}", capture=True, check=False).stdout


def apply_table(device: str, dump: str) -> None:
    """Recopie la table de partitions sur le disque destination (--init uniquement)."""
    sh(None, f"kpartx -d {device}", check=False)
    r = sh(None, f"sfdisk --no-reread {device}", capture=True, check=False, stdin=dump)
    if r.returncode != 0:
        raise RuntimeError(f"sfdisk échoué sur {device}:\n{r.stderr}")
    sh(None, f"partprobe {device}", check=False)
    time.sleep(1)


def blkid_info(host, partition: str) -> dict:
    info = {}
    for key in ("TYPE", "UUID"):
        r = sh(host, f"blkid -o value -s {key} '{partition}'", capture=True, check=False)
        info[key.lower()] = r.stdout.strip()
    return info


def mkfs(fstype: str, device: str, uuid: str) -> None:
    if fstype == "ext4":
        sh(None, f"mkfs.ext4 -q -F -O ^metadata_csum,^metadata_csum_seed,^64bit,^orphan_file "
                f"-U {uuid} {device}")
    elif fstype == "xfs":
        sh(None, f"mkfs.xfs -q -f {device}")
        if uuid:
            sh(None, f"xfs_admin -U {uuid} {device}", check=False)
    elif fstype == "swap":
        sh(None, f"mkswap -U {uuid} {device}")
    else:
        raise ValueError(f"Filesystem '{fstype}' non géré (ext4/xfs/swap uniquement).")


# ---------------------------------------------------------------------------
# Montage + rsync
# ---------------------------------------------------------------------------

def mount_remote_ro(host, device: str, mountpoint: str) -> None:
    sh(host, f"mkdir -p '{mountpoint}'; umount '{mountpoint}' 2>/dev/null; "
             f"mount -o ro '{device}' '{mountpoint}'")


def umount_remote(host, mountpoint: str) -> None:
    sh(host, f"umount '{mountpoint}'", check=False)


def mount_local(device: str, mountpoint: str) -> None:
    os.makedirs(mountpoint, exist_ok=True)
    sh(None, f"umount '{mountpoint}' 2>/dev/null", check=False)
    sh(None, f"mount '{device}' '{mountpoint}'")


def umount_local(mountpoint: str) -> None:
    sh(None, f"umount '{mountpoint}'", check=False)


def rsync_pull(host: str, src_mnt: str, dst_mnt: str) -> None:
    r = subprocess.run(
        ["rsync", "-aHAX", "--delete", "--numeric-ids", "-e", "ssh",
         f"{host}:{src_mnt}/", f"{dst_mnt}/"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"rsync {host}:{src_mnt} → {dst_mnt} échoué:\n{r.stderr}")


# ---------------------------------------------------------------------------
# Synchronisation d'un disque
# ---------------------------------------------------------------------------

def sync_disk(source_host: str, disk_idx: int, src_volume: str, dst_volume: str,
             init: bool) -> None:
    src_dev = disk_path(source_host, src_volume)
    dst_dev = disk_path(None, dst_volume)
    print(f"  Disque {disk_idx} : {source_host}:{src_dev} ({src_volume}) → "
          f"{dst_dev} ({dst_volume})")

    if init:
        dump = sfdisk_dump(source_host, src_dev)
        if dump.strip():
            apply_table(dst_dev, dump)

    src_parts = kpartx_add(source_host, src_dev)
    dst_parts = kpartx_add(None, dst_dev)

    try:
        if len(src_parts) != len(dst_parts):
            raise RuntimeError(
                f"nombre de partitions différent (source={len(src_parts)}, "
                f"destination={len(dst_parts)}) — relancez avec --init.")

        for p_idx, (sp, dp) in enumerate(zip(src_parts, dst_parts)):
            info = blkid_info(source_host, sp)
            fstype, uuid = info["type"], info["uuid"]

            if not fstype:
                continue

            if fstype == "swap":
                if init:
                    mkfs("swap", dp, uuid)
                continue

            if fstype not in ("ext4", "xfs"):
                print(f"    Attention : filesystem '{fstype}' non géré, "
                     f"partition {sp} ignorée.", file=sys.stderr)
                continue

            if init:
                mkfs(fstype, dp, uuid)

            src_mnt = f"{SRC_MOUNT_BASE}/disk{disk_idx}-p{p_idx}"
            dst_mnt = f"{DST_MOUNT_BASE}/disk{disk_idx}-p{p_idx}"

            mount_remote_ro(source_host, sp, src_mnt)
            mount_local(dp, dst_mnt)
            try:
                rsync_pull(source_host, src_mnt, dst_mnt)
            finally:
                umount_local(dst_mnt)
                umount_remote(source_host, src_mnt)
    finally:
        kpartx_remove(None, dst_dev)
        kpartx_remove(source_host, src_dev)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Migration VM Proxmox → Proxmox")
    parser.add_argument("source_host", help="Serveur Proxmox source (alias SSH ou IP)")
    parser.add_argument("vm_name", help="Nom de la VM (identique des deux côtés)")
    parser.add_argument("--create", action="store_true",
                        help="Crée la VM sur ce serveur (CPU/RAM/disques vides) "
                             "d'après la config de la VM source.")
    parser.add_argument("--storage", default="local-lvm",
                        help="Storage destination pour --create (défaut : local-lvm)")
    parser.add_argument("--bridge", default="vmbr0",
                        help="Bridge réseau destination pour --create (défaut : vmbr0)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true",
                       help="Premier passage : recopie la table de partitions, "
                            "recrée les filesystems, puis rsync complet (destructif "
                            "côté destination).")
    group.add_argument("--rsync", action="store_true",
                       help="Resynchronisation incrémentale (rsync uniquement).")
    args = parser.parse_args()

    if not (args.create or args.init or args.rsync):
        parser.error("Au moins une option parmi --create, --init, --rsync est requise.")

    check_root()
    check_tools()

    if args.create:
        create_vm(args.source_host, args.vm_name, args.storage, args.bridge)

    if not (args.init or args.rsync):
        return

    source_vmid = find_vmid(args.source_host, args.vm_name)
    if not source_vmid:
        die(f"VM '{args.vm_name}' introuvable sur {args.source_host}.")
    dest_vmid = find_vmid(None, args.vm_name)
    if not dest_vmid:
        die(f"VM '{args.vm_name}' introuvable sur ce serveur (destination). "
            f"Créez-la d'abord avec --create.")

    if get_status(args.source_host, source_vmid) != "stopped":
        die(f"la VM '{args.vm_name}' n'est pas arrêtée sur {args.source_host}.")
    if get_status(None, dest_vmid) != "stopped":
        die(f"la VM '{args.vm_name}' n'est pas arrêtée sur ce serveur.")

    src_disks = get_disks(args.source_host, source_vmid)
    dst_disks = get_disks(None, dest_vmid)
    if len(src_disks) != len(dst_disks):
        die(f"nombre de disques différent : source={len(src_disks)}, "
            f"destination={len(dst_disks)}.")

    if args.init:
        print(f"⚠️  --init va recréer la table de partitions et les filesystems "
              f"des {len(dst_disks)} disque(s) de '{args.vm_name}' sur ce serveur.")
        if input("Confirmer ? (oui/non) : ").strip() != "oui":
            print("Annulé.")
            sys.exit(0)

    start = time.time()
    print(f"VM '{args.vm_name}' : {args.source_host} (VMID={source_vmid}) → "
          f"local (VMID={dest_vmid}) — début {time.strftime('%H:%M:%S')}")

    for idx, ((_, src_vol, _), (_, dst_vol, _)) in enumerate(zip(src_disks, dst_disks)):
        sync_disk(args.source_host, idx, src_vol, dst_vol, args.init)

    elapsed = int(time.time() - start)
    print(f"Synchronisation terminée pour '{args.vm_name}' "
          f"— fin {time.strftime('%H:%M:%S')} (durée {elapsed}s)")


if __name__ == "__main__":
    main()
