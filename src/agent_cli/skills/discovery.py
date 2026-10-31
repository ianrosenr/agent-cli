"""Discovering the effective skill catalog.

Sources, least to most specific: built-in skills, ``~/.agent-cli/skills/``,
``<repo>/.agent-cli/skills/``, then each ``skills.paths`` entry in declared order. A same-named
skill from a later source replaces the earlier package whole; bodies and metadata never merge.

The catalog is a value, not a singleton: load it again to pick up changed skill files.
Discovery only reads skill packages. It never routes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_cli.skills.models import Skill, SkillError, SkillSource, SourceKind
from agent_cli.skills.parser import SKILL_FILE, parse_skill
from agent_cli.skills.tokens import DEFAULT_ESTIMATOR, TokenEstimator
from agent_cli.state.paths import AGENT_DIR

SKILLS_DIR = "skills"
POLICIES_DIR = "policies"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def builtin_root() -> Path:
    """Shipped built-ins: inside the installed package, or the repository's ``builtins/``."""
    packaged = PACKAGE_ROOT / "builtins"
    if packaged.is_dir():
        return packaged
    return PACKAGE_ROOT.parent.parent / "builtins"


def skill_sources(
    configured: Iterable[str] = (),
    project_root: Path | None = None,
    home: Path | None = None,
    builtins: Path | None = None,
) -> list[SkillSource]:
    """Every skill source in precedence order. Relative configured paths resolve against the
    repository root (or the working directory outside a repository)."""
    home = Path.home() if home is None else home
    sources = [
        SkillSource(SourceKind.BUILTIN, (builtins or builtin_root()) / SKILLS_DIR),
        SkillSource(SourceKind.GLOBAL, home / AGENT_DIR / SKILLS_DIR),
    ]
    if project_root is not None:
        sources.append(SkillSource(SourceKind.PROJECT, project_root / AGENT_DIR / SKILLS_DIR))
    base = project_root if project_root is not None else Path.cwd()
    for entry in configured:
        path = Path(entry).expanduser()
        root = path if path.is_absolute() else base / path
        sources.append(SkillSource(SourceKind.CONFIGURED, root))
    return sources


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """The effective skills after source precedence, keyed by name in name order."""

    skills: dict[str, Skill]
    estimator_id: str

    def __contains__(self, name: object) -> bool:
        return name in self.skills

    def __iter__(self) -> Iterator[Skill]:
        return iter(self.skills.values())

    def __len__(self) -> int:
        return len(self.skills)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def __getitem__(self, name: str) -> Skill:
        return self.skills[name]


def load_catalog(
    sources: Sequence[SkillSource], estimator: TokenEstimator = DEFAULT_ESTIMATOR
) -> SkillCatalog:
    """Read every source and resolve precedence. Any invalid package fails the whole load."""
    effective: dict[str, Skill] = {}
    for source in sources:
        for skill in read_source(source, estimator):
            effective[skill.name] = skill
    return SkillCatalog(dict(sorted(effective.items())), estimator.id)


def read_source(source: SkillSource, estimator: TokenEstimator) -> list[Skill]:
    """The packages directly inside one source directory, in name order.

    A missing built-in, user, or project directory is an empty source; a configured path must
    exist, because the user named it.
    """
    root = source.root
    try:
        if not root.exists():
            if source.kind is SourceKind.CONFIGURED:
                raise SkillError(f"skills.paths entry {root} does not exist")
            return []
        if not root.is_dir():
            raise SkillError(f"skill source {root} is not a directory")
        packages = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
        )
        files = [package / SKILL_FILE for package in packages]
        files = [path for path in files if path.is_file()]
    except OSError as error:
        raise SkillError(f"cannot read skill source {root}: {error}") from error
    # A package's name must match its directory, so names within one source are unique.
    skills = []
    for path in files:
        parsed = parse_skill(path)
        skills.append(
            Skill(
                parsed.name,
                parsed.description,
                parsed.version,
                source,
                path,
                parsed.body,
                estimator.estimate(parsed.body),
            )
        )
    return skills
