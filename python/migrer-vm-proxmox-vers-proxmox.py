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
            destination (normalement vides à ce stade). Réinstalle aussi
            GRUB (BIOS/MBR) automatiquement à la fin (équivalent
            --reinstall-grub).
  --rsync   Passages suivants : rsync incrémental uniquement (partitions et
            filesystems déjà en place). Ne touche pas à GRUB (déjà installé
            lors du --init).

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

CREATE_TOOLS = ["qm", "pvesm", "ssh"]
SYNC_TOOLS = CREATE_TOOLS + [
    "kpartx", "sfdisk", "partprobe", "blkid",
    "mkfs.ext4", "mkfs.xfs", "xfs_admin", "mkswap", "rsync",
    "chroot", "grub-install", "update-grub",
]
SRC_MOUNT_BASE = "/mnt/migrate-src"
DST_MOUNT_BASE = "/mnt/migrate-dst"
LOG_FILE = "/var/log/migrer-vm-proxmox-vers-proxmox.log"


# ---------------------------------------------------------------------------
# Helpers d'exécution (local ou via SSH selon host=None/host=<nom>)
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    print(f"Erreur : {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg: str) -> None:
    """Détail écrit dans LOG_FILE uniquement (pas affiché à l'écran) — utile
    pour le débogage sans polluer l'affichage résumé (2 lignes/VM)."""
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


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


def sh_log(host, command: str, *, check: bool = True,
          stdin: str = None) -> subprocess.CompletedProcess:
    """Comme sh(), mais capture stdout/stderr et les envoie dans LOG_FILE au lieu
    de les laisser s'afficher à l'écran (ex: 'update VM ...' de qm set, sortie de
    kpartx/mkfs/grub-install...). Toujours affiché : les erreurs (via l'exception
    RuntimeError levée par sh(), qui inclut déjà stderr)."""
    r = sh(host, command, check=check, capture=True, stdin=stdin)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        log(out)
    if err:
        log(err)
    return r


def check_root() -> None:
    if os.getuid() != 0:
        die("ce script doit être exécuté en root (mount/mkfs).")


def check_tools(tools: list) -> None:
    missing = [t for t in tools if not _which(t)]
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


def activate_lv(host, device: str) -> None:
    """Active le volume logique (LVM classique) si besoin : sans ça, /dev/<vg>/<lv>
    n'existe pas tant que la VM n'a pas démarré au moins une fois depuis l'activation
    (contrairement au LVM-thin, activé automatiquement par udev)."""
    m = re.match(r'^/dev/([^/]+)/(.+)$', device)
    if m:
        sh(host, f"lvchange -ay {m.group(1)}/{m.group(2)}", check=False)


def get_vm_specs(host, vmid: str):
    """Retourne (memory_mb, cores) lus depuis 'qm config'."""
    text = sh(host, f"qm config {vmid}", capture=True).stdout
    memory = re.search(r'^memory:\s*(\d+)', text, re.M)
    cores = re.search(r'^cores:\s*(\d+)', text, re.M)
    return (int(memory.group(1)) if memory else 512,
            int(cores.group(1)) if cores else 1)


def get_vm_name(host, vmid: str) -> str:
    text = sh(host, f"qm config {vmid}", capture=True).stdout
    m = re.search(r'^name:\s*(\S+)', text, re.M)
    return m.group(1) if m else ""


def slot_num(slot: str) -> int:
    return int(re.match(r'^[a-z]+(\d+)$', slot).group(1))


def volume_owner_vmid(volume: str):
    """Extrait le VMID propriétaire d'après le nom du volume (ex: 'vm-115-disk-1' -> 115).
    None si le volume ne suit pas cette convention de nommage."""
    m = re.search(r'vm-(\d+)-disk-\d+', volume)
    return m.group(1) if m else None


def volume_disk_num(volume: str):
    """Extrait le numéro de disque d'après le nom du volume (ex: 'vm-115-disk-2' -> 2).
    C'est ce numéro qu'il faut retrouver chez le propriétaire (même convention de
    nommage 'vm-<vmid>-disk-<n>' des deux côtés), PAS le numéro de slot scsi de la VM
    qui référence ce disque partagé (qui peut être différent, ex: scsi1 chez l'une,
    disk-2 chez l'autre)."""
    m = re.search(r'-disk-(\d+)', volume)
    return int(m.group(1)) if m else None


def get_nets(host, vmid: str):
    """Retourne [(slot, bridge, tag), ...] lus depuis 'qm config' (netN: ...).
    tag est None si la ligne ne précise pas de VLAN."""
    r = sh(host, f"qm config {vmid}", capture=True)
    nets = []
    for line in r.stdout.splitlines():
        m = re.match(r'^net(\d+):\s*(\S+)', line)
        if not m:
            continue
        idx, opts = m.group(1), m.group(2)
        bridge_m = re.search(r'\bbridge=([^,\s]+)', opts)
        tag_m = re.search(r'\btag=(\d+)', opts)
        if bridge_m:
            nets.append((int(idx), bridge_m.group(1), tag_m.group(1) if tag_m else None))
    nets.sort(key=lambda n: n[0])
    return nets


# ---------------------------------------------------------------------------
# --create : crée la VM destination (CPU/RAM/disques/réseau vides) d'après la source
# ---------------------------------------------------------------------------

def create_vm(source_host: str, vm_name: str, storage: str, bridge: str,
              skip_disks: frozenset = frozenset()) -> None:
    start = time.time()
    print(f"{time.strftime('%H:%M:%S')} --create {vm_name} : début")
    log(f"--create {vm_name} : début (source={source_host})")

    source_vmid = find_vmid(source_host, vm_name)
    if not source_vmid:
        die(f"VM '{vm_name}' introuvable sur {source_host}.")
    if get_status(source_host, source_vmid) != "stopped":
        die(f"la VM '{vm_name}' n'est pas arrêtée sur {source_host}.")

    memory_mb, cores = get_vm_specs(source_host, source_vmid)
    src_disks = get_disks(source_host, source_vmid)
    if not src_disks:
        die(f"VM '{vm_name}' sur {source_host} n'a aucun disque.")
    src_nets = get_nets(source_host, source_vmid)

    vmid = find_vmid(None, vm_name)
    if vmid:
        log(f"VM '{vm_name}' existe déjà (VMID={vmid}) — complète ce qui manque.")
        existing_slots = {slot for slot, _, _ in get_disks(None, vmid)}
    else:
        vmid = sh(None, "pvesh get /cluster/nextid", capture=True).stdout.strip()
        sh_log(None, f"qm create {vmid} --name {vm_name} --memory {memory_mb} --cores {cores} "
                    f"--cpu host --ostype l26 --scsihw virtio-scsi-single")
        existing_slots = set()

    for slot, volume, size in src_disks:
        if slot in skip_disks:
            log(f"  disque {slot} : {volume} ({size or '?'}) -> ignoré (--skip-disk)")
            continue
        if slot in existing_slots:
            log(f"  disque {slot} : {volume} ({size or '?'}) -> déjà présent, ignoré")
            continue
        idx = slot_num(slot)
        owner_vmid = volume_owner_vmid(volume)

        if owner_vmid and owner_vmid != source_vmid:
            # Disque partagé, appartenant à une autre VM source : on ne crée pas de
            # nouveau disque, on rattache le volume déjà créé pour cette autre VM
            # (sinon on duplique inutilement plusieurs To et on casse le partage).
            owner_name = get_vm_name(source_host, owner_vmid)
            owner_dest_vmid = find_vmid(None, owner_name)
            if not owner_dest_vmid:
                die(f"le disque '{volume}' est partagé avec la VM '{owner_name}' "
                    f"(VMID={owner_vmid} sur {source_host}), qui n'est pas encore créée "
                    f"sur ce serveur. Migrez-la d'abord (--create --init), puis relancez "
                    f"'{vm_name}'.")
            disk_num = volume_disk_num(volume)
            owner_dest_disks = get_disks(None, owner_dest_vmid)
            reused = next((v for s, v, _ in owner_dest_disks if slot_num(s) == disk_num), None)
            if not reused:
                die(f"aucun disque n°{disk_num} trouvé sur '{owner_name}' "
                    f"(VMID={owner_dest_vmid}) pour rattacher le volume partagé '{volume}'.")
            sh_log(None, f"qm set {vmid} --scsi{idx} {reused},iothread=1")
            log(f"  disque {idx} : {volume} ({size or '?'}) -> {reused} "
               f"(partagé avec '{owner_name}', rattaché)")
            continue

        size_gb = math.ceil(float(size.rstrip("GgMm"))) if size else 32
        if size and size[-1] in "Mm":
            size_gb = math.ceil(size_gb / 1024)
        sh_log(None, f"qm set {vmid} --scsi{idx} {storage}:{size_gb},iothread=1")
        log(f"  disque {idx} : {volume} ({size or '?'}) -> {storage} ({size_gb}G, vide)")

    if src_nets:
        for net_idx, net_bridge, tag in src_nets:
            tag_opt = f",tag={tag}" if tag else ""
            sh_log(None, f"qm set {vmid} --net{net_idx} virtio,bridge={net_bridge}{tag_opt},firewall=1")
            log(f"  réseau net{net_idx} : bridge={net_bridge}"
               f"{' tag=' + tag if tag else ''} (identique à la source)")
    else:
        sh_log(None, f"qm set {vmid} --net0 virtio,bridge={bridge},firewall=1")
        log(f"  réseau net0 : bridge={bridge} (aucune interface trouvée côté source, "
           f"valeur par défaut)")

    sh_log(None, f"qm set {vmid} --boot order=scsi0")
    log(f"VM '{vm_name}' créée/complétée (VMID={vmid}, {memory_mb} Mo RAM, {cores} cores).")

    elapsed = int(time.time() - start)
    print(f"{time.strftime('%H:%M:%S')} --create {vm_name} : fin (VMID={vmid}, durée {elapsed}s)")
    print()


# ---------------------------------------------------------------------------
# Partitions (kpartx local ou distant), filesystems (mkfs local uniquement)
# ---------------------------------------------------------------------------

def kpartx_add(host, device: str):
    sh_log(host, f"kpartx -avs {device}", check=False)
    r = sh(host, f"kpartx -l {device}", capture=True, check=False)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return [device]
    return ["/dev/mapper/" + l.split()[0] for l in lines]


def kpartx_remove(host, device: str) -> None:
    sh_log(host, f"kpartx -d {device}", check=False)


def sfdisk_dump(host, device: str) -> str:
    return sh(host, f"sfdisk -d {device}", capture=True, check=False).stdout


def apply_table(device: str, dump: str) -> None:
    """Recopie la table de partitions sur le disque destination (--init uniquement)."""
    sh_log(None, f"kpartx -d {device}", check=False)
    r = sh(None, f"sfdisk --no-reread {device}", capture=True, check=False, stdin=dump)
    if r.stdout.strip():
        log(r.stdout.strip())
    if r.returncode != 0:
        raise RuntimeError(f"sfdisk échoué sur {device}:\n{r.stderr}")
    sh_log(None, f"partprobe {device}", check=False)
    time.sleep(1)


def blkid_info(host, partition: str) -> dict:
    info = {}
    for key in ("TYPE", "UUID"):
        r = sh(host, f"blkid -o value -s {key} '{partition}'", capture=True, check=False)
        info[key.lower()] = r.stdout.strip()
    return info


def mkfs(fstype: str, device: str, uuid: str) -> None:
    if fstype == "ext4":
        sh_log(None, f"mkfs.ext4 -q -F -O ^metadata_csum,^metadata_csum_seed,^64bit,^orphan_file "
                    f"-U {uuid} {device}")
    elif fstype == "xfs":
        sh_log(None, f"mkfs.xfs -q -f {device}")
        if uuid:
            sh_log(None, f"xfs_admin -U {uuid} {device}", check=False)
    elif fstype == "swap":
        sh_log(None, f"mkswap -U {uuid} {device}")
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
             init: bool, empty: bool = False) -> None:
    src_dev = disk_path(source_host, src_volume)
    dst_dev = disk_path(None, dst_volume)
    log(f"  Disque {disk_idx} : {source_host}:{src_dev} ({src_volume}) → "
       f"{dst_dev} ({dst_volume})")

    activate_lv(source_host, src_dev)
    activate_lv(None, dst_dev)

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
                log(f"    Attention : filesystem '{fstype}' non géré, "
                   f"partition {sp} ignorée.")
                continue

            if init:
                mkfs(fstype, dp, uuid)

            if empty:
                log(f"    Partition {sp} : laissée vide (--empty-disk), "
                   f"contenu non copié.")
                continue

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
# --reinstall-grub : réinstalle GRUB (BIOS/MBR) sur le disque destination
# ---------------------------------------------------------------------------

def find_root_partition(parts: list):
    """Retourne la partition contenant /etc/fstab (root), ou None."""
    for p in parts:
        info = blkid_info(None, p)
        if info["type"] not in ("ext2", "ext3", "ext4", "xfs"):
            continue
        mnt = f"{DST_MOUNT_BASE}/grub-check"
        mount_local(p, mnt)
        has_fstab = os.path.exists(os.path.join(mnt, "etc/fstab"))
        umount_local(mnt)
        if has_fstab:
            return p
    return None


def reinstall_grub(vm_name: str) -> None:
    start = time.time()
    print(f"{time.strftime('%H:%M:%S')} --reinstall-grub {vm_name} : début")

    vmid = find_vmid(None, vm_name)
    if not vmid:
        die(f"VM '{vm_name}' introuvable sur ce serveur.")

    disks = get_disks(None, vmid)
    if not disks:
        die(f"VM '{vm_name}' (VMID={vmid}) n'a aucun disque.")
    _, volume, _ = disks[0]

    dev = disk_path(None, volume)
    activate_lv(None, dev)
    parts = kpartx_add(None, dev)

    try:
        root_part = find_root_partition(parts)
        if not root_part:
            die("aucune partition root (avec /etc/fstab) trouvée sur le disque destination.")

        mnt = f"{DST_MOUNT_BASE}/grub-root"
        mount_local(root_part, mnt)
        try:
            for sub in ("dev", "proc", "sys"):
                sh_log(None, f"mount --bind /{sub} {mnt}/{sub}")
            log(f"  Réinstallation de GRUB sur {dev} (root={root_part})...")
            # --force : nécessaire quand la partition 1 démarre trop près du MBR
            # (table de partitions ancienne, pas de secteur 2048 habituel) pour
            # laisser la place au core.img — grub-install refuse sinon avec
            # "embedding area is unusually small". Accepté ici (cas de migration).
            sh_log(None, f"chroot {mnt} grub-install --force {dev}")
            sh_log(None, f"chroot {mnt} update-grub")
        finally:
            for sub in ("dev", "proc", "sys"):
                sh_log(None, f"umount {mnt}/{sub}", check=False)
            umount_local(mnt)
    finally:
        kpartx_remove(None, dev)

    log(f"GRUB réinstallé pour '{vm_name}' (VMID={vmid}).")
    elapsed = int(time.time() - start)
    print(f"{time.strftime('%H:%M:%S')} --reinstall-grub {vm_name} : fin "
         f"(VMID={vmid}, durée {elapsed}s)")
    print()


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
    parser.add_argument("--skip-disk", nargs="+", default=[], metavar="SLOT",
                        help="Slot(s) à ignorer totalement (ex: scsi1), pour --create "
                             "et --init/--rsync : ni créé, ni synchronisé. Utile pour un "
                             "disque partagé qu'on ne veut pas migrer sur ce serveur.")
    parser.add_argument("--empty-disk", nargs="+", default=[], metavar="SLOT",
                        help="Slot(s) créé(s)/initialisé(s) normalement (table de "
                             "partitions + filesystem, même UUID que la source, pour "
                             "que fstab reste valide) mais SANS copier le contenu "
                             "(laissé vide). Utile pour un gros disque partagé qu'on "
                             "ne veut pas dupliquer, sans casser fstab.")
    parser.add_argument("--only-disk", nargs="+", default=[], metavar="SLOT",
                        help="Pour --init/--rsync : ne traite QUE le(s) slot(s) "
                             "indiqué(s), les autres sont laissés totalement "
                             "intouchés (ni comparés, ni resynchronisés).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--init", action="store_true",
                       help="Premier passage : recopie la table de partitions, "
                            "recrée les filesystems, puis rsync complet (destructif "
                            "côté destination).")
    group.add_argument("--rsync", action="store_true",
                       help="Resynchronisation incrémentale (rsync uniquement).")
    parser.add_argument("--reinstall-grub", action="store_true",
                        help="Réinstalle GRUB (BIOS/MBR) sur le disque destination "
                             "(chroot + grub-install + update-grub).")
    args = parser.parse_args()

    if not (args.create or args.init or args.rsync or args.reinstall_grub):
        parser.error("Au moins une option parmi --create, --init, --rsync, "
                     "--reinstall-grub est requise.")

    check_root()
    needs_sync_tools = args.init or args.rsync or args.reinstall_grub
    check_tools(SYNC_TOOLS if needs_sync_tools else CREATE_TOOLS)
    args.skip_disk = frozenset(args.skip_disk)
    args.empty_disk = frozenset(args.empty_disk)
    args.only_disk = frozenset(args.only_disk)

    if args.create:
        create_vm(args.source_host, args.vm_name, args.storage, args.bridge, args.skip_disk)

    if args.init or args.rsync:
        _sync(args)

    if args.reinstall_grub or args.init:
        reinstall_grub(args.vm_name)


def _sync(args) -> None:
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

    src_disks = [d for d in get_disks(args.source_host, source_vmid)
                if d[0] not in args.skip_disk]
    if args.only_disk:
        src_disks = [d for d in src_disks if d[0] in args.only_disk]
    dst_disks = get_disks(None, dest_vmid)
    dst_by_slot = {slot: vol for slot, vol, _ in dst_disks}
    if not args.only_disk and len(src_disks) != len(dst_disks):
        die(f"nombre de disques différent : source={len(src_disks)} (après "
            f"--skip-disk), destination={len(dst_disks)}.")

    mode = "--init" if args.init else "--rsync"
    if args.init:
        log(f"⚠️  --init recrée la table de partitions et les filesystems "
           f"des {len(src_disks)} disque(s) traité(s) de '{args.vm_name}'.")

    start = time.time()
    print(f"{time.strftime('%H:%M:%S')} {mode} {args.vm_name} : début")
    log(f"{mode} {args.vm_name} : {args.source_host} (VMID={source_vmid}) → "
       f"local (VMID={dest_vmid}) — début")

    for src_slot, src_vol, _ in src_disks:
        idx = slot_num(src_slot)
        dst_vol = dst_by_slot.get(src_slot)
        if not dst_vol:
            die(f"aucun disque au slot '{src_slot}' côté destination "
                f"(vérifiez --skip-disk / la config destination).")
        owner_vmid = volume_owner_vmid(src_vol)
        if owner_vmid and owner_vmid != source_vmid:
            log(f"  Disque {idx} : {src_vol} partagé (propriétaire VMID={owner_vmid} "
               f"sur {args.source_host}) — déjà synchronisé via cette VM, ignoré.")
            continue
        sync_disk(args.source_host, idx, src_vol, dst_vol, args.init,
                 empty=src_slot in args.empty_disk)

    elapsed = int(time.time() - start)
    log(f"{mode} {args.vm_name} : terminé (durée {elapsed}s)")
    print(f"{time.strftime('%H:%M:%S')} {mode} {args.vm_name} : fin (durée {elapsed}s)")
    print()


if __name__ == "__main__":
    main()
