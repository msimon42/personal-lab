"""Bounded link-free owned generation inspection and guarded removal."""
from __future__ import annotations

import hashlib
import shutil
import stat

from .safety import read_source_bytes, reject_link_components


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def read_generation(root):
    """Read a bounded, link-free generation before copying or replacing it."""
    files, directories, modes, total = {}, [], {}, 0
    if not root.exists():
        return files, directories, modes
    reject_link_components(root)
    modes["."] = stat.S_IMODE(root.stat().st_mode)
    for path in sorted(root.rglob("*")):
        reject_link_components(path)
        info = path.stat()
        name = path.relative_to(root).as_posix()
        modes[name] = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            directories.append(name)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            raw = read_source_bytes(root, name, max_bytes=4 * 1024 * 1024, native_paths=True)
            total += len(raw)
            files[name] = raw
        else:
            raise ValueError("The installed workflow contains a link or unsupported file; it was left untouched.")
        if len(files) + len(directories) > 1000 or total > 16 * 1024 * 1024:
            raise ValueError("The existing workflow is too large for a safe automatic update.")
    return files, directories, modes


def generation_manifest(tree):
    files, directories, modes = tree
    return {"files": {name: _hash(raw) for name, raw in files.items()}, "directories": directories, "modes": modes}



def remove_generation(path, expected):
    if path.exists():
        if generation_manifest(read_generation(path)) != expected:
            raise ValueError("An installation generation changed outside setup; its files were preserved.")
        shutil.rmtree(path)
