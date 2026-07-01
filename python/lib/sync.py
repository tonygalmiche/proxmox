"""
sync.py — Synchronisation rsync depuis une source distante (OpenNebula) vers une destination locale.

vfat : pas de permissions Unix, rsync sans -p/-o/-g.
Autres filesystems : rsync -aHAX --numeric-ids.
"""
from run import run


def rsync(host: str, src_path: str, dst_path: str, is_vfat: bool = False) -> None:
    flags = ["-rlt", "--delete"] if is_vfat else ["-aHAX", "--delete", "--numeric-ids"]
    r = run(
        ["rsync"] + flags + ["-e", "ssh", f"{host}:{src_path}/", f"{dst_path}/"],
        capture=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"rsync {host}:{src_path} → {dst_path} échoué:\n{r.stderr}")
