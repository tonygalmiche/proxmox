"""
config.py — Chargement obligatoire de config.ini.

config.ini doit être présent dans le même dossier que le script principal.
Le script s'arrête avec un message explicite si le fichier est absent ou
si une clé requise est manquante.
"""
import configparser
import os
from dataclasses import dataclass


@dataclass
class Config:
    opennebula_host: str
    nbd_device: str
    src_mount_base: str
    proxmox_storage: str
    proxmox_bridge: str
    proxmox_net_iface: str
    grub_nbd_device: str
    dst_mount_base: str


def load(path: str) -> Config:
    if not os.path.exists(path):
        raise SystemExit(
            f"Erreur : fichier de configuration introuvable : {path}\n"
            f"Copiez config.ini.example vers config.ini et renseignez les valeurs."
        )

    p = configparser.ConfigParser()
    p.read(path)

    def get(section: str, key: str) -> str:
        try:
            return p[section][key]
        except KeyError:
            raise SystemExit(f"Erreur : clé manquante dans {path} : [{section}] {key}")

    return Config(
        opennebula_host  = get("opennebula", "host"),
        nbd_device       = get("opennebula", "nbd_device"),
        src_mount_base   = get("opennebula", "src_mount_base"),
        proxmox_storage  = get("proxmox",    "storage"),
        proxmox_bridge   = get("proxmox",    "bridge"),
        proxmox_net_iface= get("proxmox",    "net_iface"),
        grub_nbd_device  = get("proxmox",    "grub_nbd_device"),
        dst_mount_base   = get("proxmox",    "dst_mount_base"),
    )
