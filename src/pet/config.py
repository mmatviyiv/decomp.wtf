"""Project configuration: pet.toml next to the reconstructed sources."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Checks:
    build: str = ""
    test: str = ""
    # Optional per-unit verifier: exit 0 means this unit is verified. The
    # command receives PET_UNIT_ADDRESS / PET_UNIT_NAME in its environment.
    verify: str = ""

    def command(self, name: str) -> str:
        return {"build": self.build, "test": self.test, "verify": self.verify}.get(name, "")


@dataclass
class ProjectConfig:
    name: str = ""
    binary: str = ""
    source_roots: list[str] = field(default_factory=lambda: ["Sources"])
    checks: Checks = field(default_factory=Checks)

    @classmethod
    def load(cls, root: Path) -> "ProjectConfig":
        path = root / "pet.toml"
        if not path.exists():
            return cls(name=root.name)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        project = data.get("project", {})
        checks = Checks(**data.get("checks", {}))
        return cls(
            name=project.get("name") or root.name,
            binary=project.get("binary", ""),
            source_roots=list(project.get("source_roots") or ["Sources"]),
            checks=checks,
        )
