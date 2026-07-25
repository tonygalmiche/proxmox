"""Wrappers subprocess : commandes locales et SSH."""
import subprocess
from typing import Optional

from logutil import log


def run(cmd: list, *, check: bool = True, capture: bool = False,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Capture toujours stdout/stderr (jamais laissé filer sur la console) :
    en cas de succès, le détail va dans le fichier de log ; en cas d'échec
    avec check=True, il est repris dans l'exception levée."""
    result = subprocess.run(
        [str(c) for c in cmd],
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Échec (code {result.returncode}): {' '.join(str(c) for c in cmd)}"
            + (f"\n{stderr}" if stderr else "")
        )
    if stdout:
        log(stdout)
    if stderr:
        log(stderr)
    return result


def ssh(host: str, cmd: str, *, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    return run(["ssh", host, cmd], check=check, capture=capture)


def ssh_script(host: str, script: str, *args: str,
               check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Lance un script bash sur l'hôte distant via stdin."""
    return run(
        ["ssh", host, "bash", "-s", "--"] + list(args),
        check=check, capture=capture, stdin=script,
    )
