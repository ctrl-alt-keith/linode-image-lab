"""One protected, per-attempt registry version selection for a dry-run."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass


class VersionSelectionError(ValueError):
    """The attempt input cannot safely select an exact version."""


@dataclass(frozen=True)
class RegistryVersionSelection:
    version_id: str
    proof_key: bytes

    def proof(self) -> str:
        return version_proof(self.version_id, self.proof_key)


def version_proof(version_id: str, proof_key: bytes) -> str:
    return hmac.new(proof_key, b"cak-367-registry-version\0" + version_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def load_selection(path_text: str) -> RegistryVersionSelection:
    """Read the fixed-path input once; never include its bytes in diagnostics."""
    try:
        path = Path(path_text)
        if not path.is_absolute() or path.parent.is_symlink():
            raise ValueError()
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(path.parent.resolve(strict=True)):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096
                    or info.st_uid not in (0, os.getuid()) or mode & 0o027
                    or not mode & 0o400):
                raise ValueError()
            current = path.resolve(strict=True)
            named = path.stat()
            if (not current.is_relative_to(path.parent.resolve(strict=True))
                    or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError()
            raw = stream.read(4097)
        if not raw or len(raw) > 4096:
            raise ValueError()
        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {"version_id", "proof_key"}:
            raise ValueError()
        version = value["version_id"]
        key = value["proof_key"]
        if (not isinstance(version, str) or not version or version == "null"
                or len(version.encode("utf-8")) > 1024
                or any(ord(char) < 33 or ord(char) == 127 for char in version)
                or not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)):
            raise ValueError()
        return RegistryVersionSelection(version, bytes.fromhex(key))
    except (OSError, ValueError, UnicodeError):
        raise VersionSelectionError("protected registry version selection is invalid") from None
