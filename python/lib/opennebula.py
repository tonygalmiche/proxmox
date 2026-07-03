"""
opennebula.py — Interrogation de l'API OpenNebula via SSH
(onevm list/show -x, onetemplate list/show -x, oneimage show -x).

Expose find_vm_or_template() qui retourne un objet OnVm avec la liste des
disques, et is_stopped() pour vérifier l'état avant toute opération disque.

Certaines VM (ex. vm-freedom) n'ont pas d'instance onevm propre : leurs
disques (images persistantes) sont attachés à l'instance d'une autre VM
(ex. vm-rsync), donc introuvables via `onevm show`. Dans ce cas on retombe
sur le template `onetemplate` de même nom, dont les disques référencent des
IMAGE par nom plutôt qu'un chemin déjà attaché ; le chemin et la taille
réels sont alors résolus via `oneimage show`.
"""
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

from run import ssh

# États OpenNebula acceptables pour une migration (VM éteinte)
_STOPPED_STATES = {"4", "8", "9"}  # STOPPED, POWEROFF, UNDEPLOYED


@dataclass
class OnDisk:
    disk_id: int
    image: str
    source: str
    size_mb: int
    # Renseigné uniquement pour un disque de template dont l'image est une
    # image persistante actuellement attachée à une autre VM onevm déjà
    # migrée (ex: vm-freedom-samba détenue par vm-rsync). Permet à --create
    # de réutiliser le disque Proxmox existant plutôt que d'en dupliquer un.
    owner_vm_name: Optional[str] = None
    owner_disk_id: Optional[int] = None


@dataclass
class OnVm:
    vm_id: str
    name: str
    state: str
    vcpu: int
    memory_mb: int
    disks: List[OnDisk] = field(default_factory=list)
    is_template: bool = False

    def is_stopped(self) -> bool:
        # Un template n'a pas d'instance propre à arrêter ; la sécurité vient
        # de _check_image_available() (aucune VM détenant l'image ne tourne).
        return self.is_template or self.state in _STOPPED_STATES


def find_vm_or_template(host: str, name: str) -> OnVm:
    """Trouve une VM par instance onevm, ou à défaut par template onetemplate
    (cas d'une VM dont les disques sont attachés à l'instance d'une autre)."""
    try:
        return find_vm(host, name)
    except RuntimeError as exc:
        if "introuvable" not in str(exc):
            raise
    try:
        return find_template(host, name)
    except RuntimeError as exc:
        if "introuvable" not in str(exc):
            raise
        raise RuntimeError(
            f"Ni VM ni template '{name}' trouvé sur OpenNebula ({host})."
        )


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


def find_template(host: str, name: str) -> OnVm:
    """Trouve un template par nom exact ou préfixe name-<id>."""
    r = ssh(host, "onetemplate list -x", capture=True)
    root = ET.fromstring(r.stdout)

    matches = [
        (t.findtext("ID", ""), t.findtext("NAME", ""))
        for t in root.findall("VMTEMPLATE")
        if _name_matches(t.findtext("NAME", ""), name)
    ]

    if not matches:
        raise RuntimeError(f"Template '{name}' introuvable sur OpenNebula ({host}).")
    if len(matches) > 1:
        raise RuntimeError(
            f"Plusieurs templates correspondent à '{name}' : "
            + ", ".join(m[1] for m in matches)
        )

    tmpl_id, tmpl_name = matches[0]
    r = ssh(host, f"onetemplate show {tmpl_id} -x", capture=True)
    return _parse_template(host, r.stdout, tmpl_id, tmpl_name)


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


def _parse_template(host: str, xml_str: str, tmpl_id: str, tmpl_name: str) -> OnVm:
    root = ET.fromstring(xml_str)
    tmpl = root.find("TEMPLATE") or ET.Element("TEMPLATE")

    vcpu_s = tmpl.findtext("VCPU") or tmpl.findtext("CPU") or "1"
    vcpu = max(1, math.ceil(float(vcpu_s)))
    memory_mb = int(tmpl.findtext("MEMORY", "1024"))

    disks = []
    for idx, d in enumerate(tmpl.findall("DISK")):
        image_name = d.findtext("IMAGE", "")
        if not image_name:
            continue
        source, size_mb, vm_ids = _resolve_image(host, image_name)
        owner = _locate_image_owner(host, image_name, vm_ids)
        owner_vm_name, owner_disk_id = owner if owner else (None, None)
        disks.append(OnDisk(disk_id=idx, image=image_name, source=source, size_mb=size_mb,
                            owner_vm_name=owner_vm_name, owner_disk_id=owner_disk_id))

    return OnVm(vm_id=f"tmpl-{tmpl_id}", name=tmpl_name, state="",
                vcpu=vcpu, memory_mb=memory_mb, disks=disks, is_template=True)


def _resolve_image(host: str, image_name: str):
    """Retourne (source, size_mb, vm_ids) pour l'image nommée image_name.
    vm_ids : VM(s) détenant actuellement cette image persistante."""
    r = ssh(host, f"oneimage show '{image_name}' -x", capture=True)
    root = ET.fromstring(r.stdout)
    source = root.findtext("SOURCE", "")
    size_mb = int(root.findtext("SIZE", "0"))
    vm_ids = [vm_id.text for vm_id in root.findall("VMS/ID") if vm_id.text]
    return source, size_mb, vm_ids


def _locate_image_owner(host: str, image_name: str,
                        vm_ids: List[str]) -> Optional[tuple]:
    """Vérifie qu'aucune VM détenant actuellement cette image persistante
    n'est démarrée (sinon on risque de lire un disque en cours d'écriture),
    et retourne (nom_vm, disk_id) de la VM qui la détient, pour que --create
    puisse réutiliser son disque Proxmox au lieu d'en créer un nouveau.
    None si l'image n'est détenue par aucune VM (jamais migrée nulle part)."""
    for vm_id in vm_ids:
        r = ssh(host, f"onevm show {vm_id} -x", capture=True, check=False)
        if r.returncode != 0:
            continue
        vroot = ET.fromstring(r.stdout)
        vstate = vroot.findtext("STATE", "")
        vname = vroot.findtext("NAME", "")
        if vstate not in _STOPPED_STATES:
            raise RuntimeError(
                f"Image '{image_name}' détenue par la VM '{vname}' (ID={vm_id}) "
                f"qui n'est pas arrêtée (état={vstate}) — synchronisation bloquée "
                f"pour éviter de lire un disque en cours d'écriture."
            )
        vtmpl = vroot.find("TEMPLATE") or ET.Element("TEMPLATE")
        for d in vtmpl.findall("DISK"):
            if d.findtext("IMAGE", "") == image_name:
                return vname, int(d.findtext("DISK_ID", "0"))
    return None
