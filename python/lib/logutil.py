"""logutil.py — Détail des opérations écrit dans /var/log.

log() : détail (sortie des commandes, avertissements non bloquants) — va
        uniquement dans le fichier de log, jamais à l'écran.
announce() : les 2 lignes début/fin de chaque commande — va à la fois à
        l'écran et dans le fichier de log, pour qu'on retrouve l'historique
        complet des exécutions dans le log.
"""
import time

LOG_FILE = "/var/log/migrer-vm-opennebula-vers-proxmox.log"


def log(msg: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def announce(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def separator() -> None:
    """Ligne vide à l'écran et dans le log, pour séparer les VM migrées."""
    print()
    with open(LOG_FILE, "a") as f:
        f.write("\n")
