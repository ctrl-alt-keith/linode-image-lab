"""Safe user-data file loading for Linode metadata payloads."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path


class UserDataError(ValueError):
    """Raised when user-data input cannot safely be loaded."""


@dataclass(frozen=True)
class DeployUserData:
    encoded: str = field(repr=False)
    byte_count: int
    source: str = "file"


def load_user_data_file(path: str, label: str) -> DeployUserData:
    user_data_path = Path(path).expanduser()
    try:
        data = user_data_path.read_bytes()
    except FileNotFoundError as exc:
        raise UserDataError(f"{label} file not found") from exc
    except OSError as exc:
        raise UserDataError(f"{label} file could not be read") from exc

    if not data:
        raise UserDataError(f"{label} must not be empty")
    if b"\x00" in data:
        raise UserDataError(f"{label} must be text user data, not binary data")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserDataError(f"{label} must be UTF-8 text user data") from exc

    return DeployUserData(
        encoded=base64.b64encode(data).decode("ascii"),
        byte_count=len(data),
    )
