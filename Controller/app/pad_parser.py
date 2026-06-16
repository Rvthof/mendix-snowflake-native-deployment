"""
Port of Get-PadConstants from Deploy Script/deploy.ps1.

Parses etc/constants/defaults.conf and etc/constants/variables.conf from an
extracted PAD directory, returning only constants that appear in both files.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass
class PadConstant:
    name: str        # "Module.ConstantName"
    env_var: str     # env var name from variables.conf
    default: str     # default value from defaults.conf
    secret_name: str # "MX_CONST_MODULE_CONSTANTNAME"


def _parse_defaults(text: str) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^\s*"([^"]+)"\s*=\s*(.*)$', line)
        if m:
            name = m.group(1)
            val = m.group(2).strip()
            # Strip surrounding quotes
            if re.match(r'^"(.*)"$', val):
                val = val[1:-1]
            defaults[name] = val
    return defaults


def _parse_variables(text: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^\s*"([^"]+)"\s*=\s*\$\{\?([^}]+)\}', line)
        if m:
            env_vars[m.group(1)] = m.group(2)
    return env_vars


def _build_constants(defaults: dict[str, str], env_vars: dict[str, str]) -> list[PadConstant]:
    result = []
    for name, default in defaults.items():
        if name in env_vars:
            secret_name = "MX_CONST_" + name.replace(".", "_").upper()
            result.append(PadConstant(
                name=name,
                env_var=env_vars[name],
                default=default,
                secret_name=secret_name,
            ))
    return result


def parse_from_directory(pad_dir: str | Path) -> list[PadConstant]:
    pad_dir = Path(pad_dir)
    defaults_file = pad_dir / "etc" / "constants" / "defaults.conf"
    variables_file = pad_dir / "etc" / "constants" / "variables.conf"

    if not defaults_file.exists() or not variables_file.exists():
        return []

    defaults = _parse_defaults(defaults_file.read_text(encoding="utf-8"))
    env_vars = _parse_variables(variables_file.read_text(encoding="utf-8"))
    return _build_constants(defaults, env_vars)


def parse_from_zip(zip_path: str | Path) -> list[PadConstant]:
    """Parse constants from a PAD zip without fully extracting it."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        def _read(suffix: str) -> str | None:
            # Handle both flat layout and single-directory layout inside zip
            candidates = [n for n in names if n.endswith(suffix)]
            if not candidates:
                return None
            # Prefer the shortest path (closest to root)
            candidates.sort(key=len)
            with zf.open(candidates[0]) as f:
                return f.read().decode("utf-8")

        defaults_text = _read("etc/constants/defaults.conf")
        variables_text = _read("etc/constants/variables.conf")

        if defaults_text is None or variables_text is None:
            return []

        defaults = _parse_defaults(defaults_text)
        env_vars = _parse_variables(variables_text)
        return _build_constants(defaults, env_vars)
