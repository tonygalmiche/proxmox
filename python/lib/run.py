"""Wrappers subprocess : commandes locales et SSH."""
import subprocess
from typing import Optional


def run(cmd: list, *, check: bool = True, capture: bool = False,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(c) for c in cmd],
        check=False,
        capture_output=capture,
        text=True,
        input=stdin,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            f"Échec (code {result.returncode}): {' '.join(str(c) for c in cmd)}"
            + (f"\n{stderr}" if stderr else "")
        )
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
