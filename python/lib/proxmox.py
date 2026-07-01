"""
proxmox.py — Opérations Proxmox via les outils CLI (qm, pvesm, pvesh).

Création de VM, récupération de la configuration, gestion des disques
et application des paramètres post-migration (UEFI, serial).
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from run import run


@dataclass
class PveVm:
    vmid: str
    name: str
    status: str


@dataclass
class PveDisk:
    slot: str    # ex: "scsi0"
    volume: str  # ex: "local-lvm:vm-101-disk-0"


def find_vm(name: str) -> Optional[PveVm]:
    r = run(["qm", "list"], capture=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == name:
            return PveVm(vmid=parts[0], name=parts[1], status=parts[2])
    return None


def next_vmid() -> str:
    return run(["pvesh", "get", "/cluster/nextid"], capture=True).stdout.strip()


def create_vm(vmid: str, name: str, memory_mb: int, vcpu: int, bridge: str) -> None:
    run(["qm", "create", vmid,
         "--name", name,
         "--memory", str(memory_mb),
         "--cores", str(vcpu),
         "--cpu", "host",
         "--ostype", "l26",
         "--scsihw", "virtio-scsi-single",
         "--net0", f"virtio,bridge={bridge},firewall=1"])


def add_disk(vmid: str, slot_index: int, storage: str, size_gb: int) -> None:
    run(["qm", "set", vmid, f"--scsi{slot_index}", f"{storage}:{size_gb},iothread=1"])


def set_boot_disk(vmid: str) -> None:
    run(["qm", "set", vmid, "--boot", "order=scsi0"])


def get_disks(vmid: str) -> List[PveDisk]:
    r = run(["qm", "config", vmid], capture=True)
    disks = []
    for line in r.stdout.splitlines():
        m = re.match(r'^(scsi|virtio|ide|sata)(\d+):\s*(\S+)', line)
        if m and "media=cdrom" not in line:
            slot = m.group(1) + m.group(2)
            volume = m.group(3).split(",")[0]
            disks.append(PveDisk(slot=slot, volume=volume))
    disks.sort(key=lambda d: (re.match(r'^([a-z]+)(\d+)$', d.slot).group(1),
                               int(re.match(r'^([a-z]+)(\d+)$', d.slot).group(2))))
    return disks


def get_disk_path(volume: str) -> str:
    return run(["pvesm", "path", volume], capture=True).stdout.strip()


def get_status(vmid: str) -> str:
    parts = run(["qm", "status", vmid], capture=True).stdout.split()
    return parts[-1] if parts else ""


def set_uefi(vmid: str, storage: str) -> None:
    cfg = run(["qm", "config", vmid], capture=True).stdout
    if "bios: ovmf" not in cfg:
        run(["qm", "set", vmid, "--bios", "ovmf"])
        print("  UEFI : --bios ovmf appliqué")
    if "efidisk0:" not in cfg:
        run(["qm", "set", vmid, "--efidisk0",
             f"{storage}:0,efitype=4m,pre-enrolled-keys=0"])
        print("  UEFI : efidisk0 créé")


def set_serial(vmid: str) -> None:
    run(["qm", "set", vmid, "--serial0", "socket"], check=False)
