"""TOML read/write helpers — plain TOML throughout, diffable and git-friendly."""

from pathlib import Path
from typing import Any

import tomllib
import tomli_w


def read_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
