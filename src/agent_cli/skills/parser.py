"""Parsing and validating one ``SKILL.md`` package.

Metadata rules are owned by the ``creating-a-new-skill`` skill: exactly ``name``,
``description``, and ``version``; a kebab-case name matching the directory; a one-line
description of at most 120 characters; a semantic version; and a non-empty body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_cli.skills.models import SkillError

SKILL_FILE = "SKILL.md"
METADATA_KEYS = ("name", "description", "version")
MAX_DESCRIPTION = 120
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
FENCE = "---"


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    name: str
    description: str
    version: str
    body: str


def is_skill_name(name: str) -> bool:
    return NAME_PATTERN.match(name) is not None


def parse_skill(path: Path) -> ParsedSkill:
    """Read and validate ``<package>/SKILL.md``; the package directory names the skill."""
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate a byte-order mark
    except (OSError, UnicodeDecodeError) as error:
        raise SkillError(f"{path}: cannot read skill: {error}") from error
    return parse_skill_text(text, path.parent.name, str(path))


def parse_skill_text(text: str, directory: str, where: str) -> ParsedSkill:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise SkillError(f"{where}: missing metadata: SKILL.md must begin with '---'")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == FENCE)
    except StopIteration:
        raise SkillError(f"{where}: missing metadata: front matter is not closed") from None
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise SkillError(f"{where}: invalid metadata YAML: {error}") from error
    if not isinstance(metadata, dict):
        raise SkillError(f"{where}: missing metadata: expected name, description, and version")

    unknown = sorted(str(key) for key in metadata if key not in METADATA_KEYS)
    if unknown:
        raise SkillError(
            f"{where}: unknown metadata key(s) {', '.join(unknown)} "
            f"(only {', '.join(METADATA_KEYS)} are allowed)"
        )
    for key in METADATA_KEYS:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SkillError(f"{where}: metadata '{key}' must be a non-empty string")

    name: str = metadata["name"]
    description: str = metadata["description"]
    version: str = metadata["version"]
    if not is_skill_name(name):
        raise SkillError(f"{where}: name {name!r} is not lowercase kebab-case")
    if name != directory:
        raise SkillError(f"{where}: name {name!r} does not match its directory {directory!r}")
    if len([line for line in lines[1:end] if line.strip()]) != len(METADATA_KEYS):
        raise SkillError(f"{where}: each metadata value must be on one line")
    if "\n" in description or "\r" in description:
        raise SkillError(f"{where}: description must be one line")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(
            f"{where}: description is {len(description)} characters (at most {MAX_DESCRIPTION})"
        )
    if not SEMVER_PATTERN.match(version):
        raise SkillError(f"{where}: version {version!r} is not a semantic version")

    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise SkillError(f"{where}: skill body is empty")
    return ParsedSkill(name, description.strip(), version, body + "\n")
