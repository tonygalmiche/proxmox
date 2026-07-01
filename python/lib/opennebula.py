"""
opennebula.py — Interrogation de l'API OpenNebula via SSH (onevm list/show -x).

Expose find_vm() qui retourne un objet OnVm avec la liste des disques,
et is_stopped() pour vérifier l'état avant toute opération disque.
"""
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List

from run import ssh

# États OpenNebula acceptables pour une migration (VM éteinte)
_STOPPED_STATES = {"4", "8", "9"}  # STOPPED, POWEROFF, UNDEPLOYED


@dataclass
class OnDisk:
    disk_id: int
    image: str
    source: str
    size_mb: int


@dataclass
class OnVm:
    vm_id: str
    name: str
    state: str
    vcpu: int
    memory_mb: int
    disks: List[OnDisk] = field(default_factory=list)

    def is_stopped(self) -> bool:
        return self.state in _STOPPED_STATES


def find_vm(host: str, name: str) -> OnVm:
    """Trouve une VM par nom exact ou préfixe name-<id>."""
    r = ssh(host, "onevm list -x", capture=True)
    root = ET.fromstring(r.stdout)

    matches = [
        (vm.findtext("ID", ""), vm.findtext("NAME", ""))
        for vm in root.findall("VM")
        if _name_matches(vm.findtext("NAME", ""), name)
    ]

    if not matches:
        raise RuntimeError(f"VM '{name}' introuvable sur OpenNebula ({host}).")
    if len(matches) > 1:
        raise RuntimeError(
            f"Plusieurs VM correspondent à '{name}' : "
            + ", ".join(m[1] for m in matches)
        )

    vm_id, vm_name = matches[0]
    r = ssh(host, f"onevm show {vm_id} -x", capture=True)
    return _parse_vm(r.stdout, vm_id, vm_name)


def _name_matches(vm_name: str, search: str) -> bool:
    return vm_name == search or (
        vm_name.startswith(search + "-") and vm_name[len(search) + 1:].isdigit()
    )


def _parse_vm(xml_str: str, vm_id: str, vm_name: str) -> OnVm:
    root = ET.fromstring(xml_str)
    state = root.findtext("STATE", "")
    tmpl = root.find("TEMPLATE") or ET.Element("TEMPLATE")

    vcpu_s = tmpl.findtext("VCPU") or tmpl.findtext("CPU") or "1"
    vcpu = max(1, math.ceil(float(vcpu_s)))
    memory_mb = int(tmpl.findtext("MEMORY", "1024"))

    disks = []
    for d in sorted(tmpl.findall("DISK"), key=lambda x: int(x.findtext("DISK_ID", "0"))):
        disks.append(OnDisk(
            disk_id=int(d.findtext("DISK_ID", "0")),
            image=d.findtext("IMAGE", ""),
            source=d.findtext("SOURCE", ""),
            size_mb=int(d.findtext("SIZE", "0")),
        ))

    return OnVm(vm_id=vm_id, name=vm_name, state=state,
                vcpu=vcpu, memory_mb=memory_mb, disks=disks)
