"""Safe cross-platform directory link management."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def link_state(source: Path, destination: Path) -> str:
    if destination.is_symlink():
        try:
            return "linked" if destination.resolve() == source.resolve() else "wrong-link"
        except OSError:
            return "broken-link"
    if destination.exists():
        if os.name == "nt" and _same_file(source, destination):
            return "linked"
        return "conflict"
    return "missing"


def create_directory_link(source: Path, destination: Path) -> str:
    state = link_state(source, destination)
    if state == "linked":
        return "linked"
    if state != "missing":
        raise FileExistsError(f"refusing to replace {state}: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(source, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return "junction"


def remove_directory_link(source: Path, destination: Path) -> bool:
    if link_state(source, destination) != "linked":
        return False
    if destination.is_symlink():
        destination.unlink()
    elif os.name == "nt":
        destination.rmdir()
    else:
        return False
    return True


def _same_file(source: Path, destination: Path) -> bool:
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False
