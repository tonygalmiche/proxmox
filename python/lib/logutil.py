"""logutil.py — Détail des opérations écrit dans /var/log, jamais à l'écran.

La console n'affiche que le début/fin de chaque commande (2 lignes) ; tout
le détail (sortie des commandes, avertissements non bloquants) va ici.
"""
import time

LOG_FILE = "/var/log/migrer-vm-opennebula-vers-proxmox.log"


def log(msg: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
