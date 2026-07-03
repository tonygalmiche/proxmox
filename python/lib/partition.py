"""
partition.py — Gestion des tables de partitions et mappings kpartx.

Applique une table sfdisk sur un device cible, expose les partitions via
kpartx, et compare les tables source/destination avant toute copie.
"""
import re
import time
from typing import List, Optional, Tuple

import nbd as nbd_mod
import opennebula as on_mod
import proxmox as pve
from run import run, ssh

# Espace minimal (secteurs) pour accueillir une partition BIOS Boot (ef02).
_MIN_BIOS_BOOT_SECTORS = 64

_BIOS_BOOT_TYPE_RE = re.compile(r'type=\s*21686148-6449-6[Ee]6[Ff]-744[Ee]-656564454649')


def apply_table(device: str, sfdisk_dump: str) -> None:
    """Recopie la table de partitions sur device (--init uniquement)."""
    run(["kpartx", "-d", device], check=False)
    # --no-reread : le disque peut être "en cours d'utilisation" par des dm stales
    # du run précédent ; kpartx -avs relira les partitions juste après.
    r = run(["sfdisk", "--no-reread", device], stdin=sfdisk_dump, capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"sfdisk échoué sur {device}:\n{r.stderr}")
    run(["partprobe", device], check=False)
    time.sleep(1)


def kpartx_add(device: str) -> List[str]:
    """Expose les partitions via kpartx. Retourne la liste /dev/mapper/... (ou [device]).

    On lit la liste des partitions via `kpartx -l` (état actuel du device),
    plutôt que de parser le stdout de `kpartx -avs` : celui-ci n'affiche une
    ligne "add map" que pour un mapping nouvellement créé. Si le mapping
    existe déjà (résidu d'un run précédent, udev, etc.), `-avs` réussit sans
    rien afficher sur stdout, et on retombait alors sur [device] — faisant
    croire à tort que le disque de destination n'a pas de table de
    partitions du tout (cf. RuntimeError "table de partitions différente").
    """
    run(["kpartx", "-avs", device], check=False)
    r = run(["kpartx", "-l", device], capture=True, check=False)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        return [device]
    return ["/dev/mapper/" + l.split()[0] for l in lines]


def kpartx_remove(device: str) -> None:
    run(["kpartx", "-d", device], check=False)


def normalize(sfdisk_dump: str) -> List[str]:
    """Extrait les tuples start,size,type d'un dump sfdisk pour comparaison."""
    result = []
    for line in sfdisk_dump.splitlines():
        if not line.startswith("/"):
            continue
        # sfdisk peut écrire "start= 2048" (espace après =) ou "start=2048"
        start = size = ptype = ""
        m = re.search(r'\bstart=\s*(\d+)', line)
        if m:
            start = m.group(1)
        m = re.search(r'\bsize=\s*(\d+)', line)
        if m:
            size = m.group(1)
        m = re.search(r'\btype=\s*(\S+?)(?:,|\s|$)', line)
        if m:
            ptype = m.group(1)
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


def find_bios_boot_gap(device: str) -> Optional[Tuple[int, int]]:
    """Cherche l'espace libre entre le header GPT (secteur 34) et la 1re
    partition, pour y créer une partition BIOS Boot (ef02) sans toucher
    aux partitions existantes. Retourne (start, end) ou None si le disque
    n'est pas en GPT, a déjà une ef02, ou n'a pas assez de place."""
    dump = get_local_dump(device)
    if "label: gpt" not in dump:
        return None
    if _BIOS_BOOT_TYPE_RE.search(dump):
        return None  # BIOS Boot Partition déjà présente

    first_start = None
    for line in dump.splitlines():
        if not line.startswith("/dev/"):
            continue
        m = re.search(r'\bstart=\s*(\d+)', line)
        if m:
            start = int(m.group(1))
            if first_start is None or start < first_start:
                first_start = start

    if first_start is None:
        return None

    gap_start = 34
    gap_end = first_start - 1
    if gap_end - gap_start + 1 < _MIN_BIOS_BOOT_SECTORS:
        return None
    return (gap_start, gap_end)


def add_bios_boot_partition(device: str, start: int, end: int) -> None:
    """Ajoute une partition BIOS Boot (ef02) sur l'espace libre [start, end].

    Le header GPT existant a été écrit par util-linux fdisk avec
    FirstUsableLBA=2048 stocké tel quel (alignement 1 MiB), indépendamment
    de tout calcul dynamique. --set-alignment=1 seul ne le fait PAS
    recalculer. Il faut aussi passer --move-main-table=2 (déplace la table
    principale vers sa position actuelle) : cette opération force gdisk à
    recalculer FirstUsableLBA à partir de l'alignement courant, le faisant
    passer de 2048 à 34 et libérant le gap. Vérifié avec sgdisk --pretend :
    sans --move-main-table, --new=0:34:2047 échoue toujours même avec
    alignement=1 ; avec, First usable sector devient 34.
    """
    run(["sgdisk", "--set-alignment=1", "--move-main-table=2",
         f"--new=0:{start}:{end}", "--typecode=0:ef02",
         "--change-name=0:BIOS boot partition", device])
    run(["partprobe", device], check=False)
    time.sleep(1)


def bios_boot_partition_numbers(device: str) -> set:
    """Numéros de partition (1-based) de type BIOS Boot (ef02) sur device.

    Ajoutée par reinstall-grub, cette partition n'existe jamais côté source
    OpenNebula : il faut l'exclure des comparaisons/copies source↔destination."""
    numbers = set()
    for line in get_local_dump(device).splitlines():
        if not line.startswith("/dev/"):
            continue
        if _BIOS_BOOT_TYPE_RE.search(line):
            m = re.match(rf'^{re.escape(device)}p?(\d+)\s*:', line)
            if m:
                numbers.add(int(m.group(1)))
    return numbers


def strip_bios_boot(dump: str) -> str:
    """Retire du dump sfdisk les lignes de type BIOS Boot (ef02)."""
    return "\n".join(
        line for line in dump.splitlines()
        if not (line.startswith("/dev/") and _BIOS_BOOT_TYPE_RE.search(line))
    )


def check_consistency(host: str, nbd_device: str, sfdisk_dump: str,
                      src_parts: List[str], pve_dev: str,
                      dst_parts: List[str]) -> Tuple[bool, bool, bool, str]:
    """Compare les tables de partitions source/destination sans jamais rien
    modifier. Retourne (has_table_src, has_table_dst, ok, détail)."""
    has_table_src = len(src_parts) > 1 or src_parts[0] != nbd_device
    has_table_dst = dst_parts[0] != pve_dev

    if len(src_parts) != len(dst_parts):
        return has_table_src, has_table_dst, False, (
            f"nombre de partitions différent (source={len(src_parts)}, "
            f"destination={len(dst_parts)})"
        )

    if has_table_src != has_table_dst:
        return has_table_src, has_table_dst, False, (
            "table de partitions présente d'un seul côté "
            f"(source={'oui' if has_table_src else 'non'}, "
            f"destination={'oui' if has_table_dst else 'non'})"
        )

    if has_table_src:
        dst_dump = strip_bios_boot(get_local_dump(pve_dev))
        if normalize(sfdisk_dump) != normalize(dst_dump):
            return has_table_src, has_table_dst, False, "tables de partitions différentes"
        return has_table_src, has_table_dst, True, "table de partitions identique"

    src_size = get_remote_size(host, nbd_device)
    dst_size = get_size(pve_dev)
    if src_size != dst_size:
        return has_table_src, has_table_dst, False, (
            f"pas de table des deux côtés mais tailles différentes "
            f"(source={src_size}, destination={dst_size})"
        )
    return has_table_src, has_table_dst, True, "pas de table de partitions (disque brut), taille identique"


def diagnose_disk(host: str, source: str, nbd_device: str, pve_dev: str,
                  mount_base: str) -> Tuple[bool, bool, bool, str]:
    """État en lecture seule d'un disque : connecte la source en NBD
    read-only, ajoute/retire un mapping kpartx destination (réversible,
    n'applique aucune table ni mkfs), et retourne
    (has_table_src, has_table_dst, ok, détail)."""
    sfdisk_dump, src_parts = nbd_mod.remote_connect(host, source, nbd_device, mount_base)
    try:
        dst_parts = kpartx_add(pve_dev)
        bios_boot_nums = bios_boot_partition_numbers(pve_dev)
        if bios_boot_nums:
            dst_parts = [p for p in dst_parts
                        if not (m := re.search(r'\d+$', p))
                        or int(m.group()) not in bios_boot_nums]
        return check_consistency(host, nbd_device, sfdisk_dump, src_parts, pve_dev, dst_parts)
    finally:
        kpartx_remove(pve_dev)
        nbd_mod.remote_disconnect(host, nbd_device, mount_base)


def print_disks_state(vm_name: str, cfg) -> None:
    """Affiche pour chaque disque de la VM si sa table de partitions Proxmox
    correspond à la source OpenNebula, ou s'il faut le réinitialiser
    (--init DISK_ID). Purement diagnostique : voir diagnose_disk()."""
    on_vm = on_mod.find_vm_or_template(cfg.opennebula_host, vm_name)

    pve_vm = pve.find_vm(vm_name)
    if not pve_vm:
        raise RuntimeError(f"VM Proxmox '{vm_name}' introuvable. Lancez d'abord --create.")

    pve_disks = pve.get_disks(pve_vm.vmid)
    if len(pve_disks) != len(on_vm.disks):
        raise RuntimeError(f"nombre de disques différent : OpenNebula={len(on_vm.disks)}, "
                           f"Proxmox={len(pve_disks)}.")

    print(f"VM '{vm_name}' : OpenNebula ID={on_vm.vm_id} ↔ Proxmox VMID={pve_vm.vmid} "
          f"— état des {len(on_vm.disks)} disque(s)\n")
    print(f"{'DISK':<6} {'TABLE SRC':<11} {'TABLE DST':<11} {'À INITIALISER':<15} DÉTAIL")

    to_init = []
    for idx, (on_disk, pve_disk) in enumerate(zip(on_vm.disks, pve_disks)):
        nbd_device = nbd_mod.pick_device(cfg.nbd_device, idx)
        pve_dev = pve.get_disk_path(pve_disk.volume)
        try:
            has_table_src, has_table_dst, ok, detail = diagnose_disk(
                cfg.opennebula_host, on_disk.source, nbd_device, pve_dev, cfg.src_mount_base
            )
        except Exception as exc:
            has_table_src, has_table_dst, ok, detail = False, False, False, f"erreur : {exc}"

        needs_init = not ok
        print(f"{on_disk.disk_id:<6} {'oui' if has_table_src else 'non':<11} "
              f"{'oui' if has_table_dst else 'non':<11} {'OUI' if needs_init else 'non':<15} {detail}")
        if needs_init:
            to_init.append(on_disk.disk_id)

    if to_init:
        flags = " ".join(str(i) for i in to_init)
        print(f"\nDisque(s) à réinitialiser : --init {flags}")
    else:
        print("\nTous les disques sont cohérents, aucun --init nécessaire.")


def flush(device: str, partitions: List[str]) -> None:
    """Flush tous les buffers avant de démonter kpartx (évite les données fantômes)."""
    run(["sync"])
    for p in partitions:
        run(["blockdev", "--flushbufs", p], check=False)
    run(["blockdev", "--flushbufs", device], check=False)
    run(["sync"])
