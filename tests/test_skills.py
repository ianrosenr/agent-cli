"""Skill packages, discovery, source precedence, token estimation, and the routing policy."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli.skills import (
    SkillError,
    SourceKind,
    Utf8ByteEstimator,
    builtin_root,
    load_catalog,
    load_policy,
    skill_sources,
)
from agent_cli.skills.discovery import read_source
from agent_cli.skills.models import SkillSource
from agent_cli.skills.parser import parse_skill_text
from agent_cli.skills.policy import default_policy_path, parse_policy

INITIAL_BUILTINS = {
    "software-engineering",
    "testing",
    "specification",
    "review",
    "architecture",
    "research",
    "security",
    "interfaces",
    "data",
    "observability",
    "creating-a-new-skill",
    "skill-evaluation",
}

CANONICAL_ROLE_DEFAULTS = {
    "orchestrator": (),
    "spec_author": ("specification",),
    "spec_reviewer": ("review", "specification"),
    "architect": ("architecture",),
    "implementer": ("software-engineering", "testing"),
    "code_reviewer": ("review", "software-engineering"),
    "research_investigator": ("research",),
    "security_investigator": ("security",),
    "skill_author": ("creating-a-new-skill",),
    "skill_evaluator": ("skill-evaluation",),
}


def package(
    root: Path,
    name: str,
    body: str = "Rules.\n",
    *,
    version: str = "1.0.0",
    description: str = "Rules for something.",
    directory: str | None = None,
    extra: str = "",
) -> Path:
    path = root / (directory or name) / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\n{extra}---\n\n{body}",
        encoding="utf-8",
    )
    return path


def sources(home: Path, project: Path | None = None, *configured: str) -> list[SkillSource]:
    return skill_sources(configured, project, home)


# -- Built-ins --------------------------------------------------------------------------------


def test_builtin_skills_are_valid_and_include_the_initial_packages(tmp_path: Path) -> None:
    catalog = load_catalog(sources(tmp_path))
    assert set(catalog.skills) >= INITIAL_BUILTINS
    assert "type-safety" not in catalog
    assert all(skill.source.kind is SourceKind.BUILTIN for skill in catalog)
    assert list(catalog.skills) == sorted(catalog.skills)


def test_builtin_skill_files_use_exactly_the_v1_metadata_keys() -> None:
    for path in (builtin_root() / "skills").glob("*/SKILL.md"):
        front = path.read_text(encoding="utf-8").split("---")[1]
        assert set(yaml.safe_load(front)) == {"name", "description", "version"}, path


def test_builtin_policy_matches_the_catalog_and_canonical_role_defaults(tmp_path: Path) -> None:
    policy = load_policy()
    policy.check_catalog(load_catalog(sources(tmp_path)))
    assert policy.role_defaults == CANONICAL_ROLE_DEFAULTS


def test_no_skill_is_a_default_for_every_role() -> None:
    defaults = load_policy().role_defaults.values()
    shared = set.intersection(*(set(skills) for skills in defaults))
    assert shared == set()


# -- Parsing ----------------------------------------------------------------------------------


def parse(text: str, directory: str = "security") -> None:
    parse_skill_text(text, directory, "SKILL.md")


VALID = "---\nname: security\ndescription: Rules.\nversion: 1.2.3\n---\n\nBody.\n"


def test_valid_skill_parses_with_body_only() -> None:
    parsed = parse_skill_text(VALID, "security", "SKILL.md")
    assert (parsed.name, parsed.description, parsed.version) == ("security", "Rules.", "1.2.3")
    assert parsed.body == "Body.\n"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("No front matter.\n", "missing metadata"),
        ("---\nname: security\n", "not closed"),
        ("---\n---\nBody.\n", "missing metadata"),
        ("---\nname: security\nversion: 1.0.0\n---\nBody\n", "'description'"),
        (VALID.replace("version: 1.2.3\n", "version: 1.2.3\ntags: [x]\n"), "unknown metadata"),
        (VALID.replace("name: security", "name: Security"), "kebab-case"),
        (VALID.replace("name: security", "name: secure"), "does not match its directory"),
        (VALID.replace("description: Rules.", "description: Rules\n  that wrap."), "one line"),
        (VALID.replace("description: Rules.", "description: " + "x" * 121), "121 characters"),
        (VALID.replace("1.2.3", "v1.2.3"), "semantic version"),
        (VALID.replace("1.2.3", "'01.2.3'"), "semantic version"),
        (VALID.replace("version: 1.2.3", "version: 1.5"), "non-empty string"),
        (VALID.replace("\nBody.\n", "\n   \n"), "body is empty"),
        ("---\n: [\n---\nBody\n", "invalid metadata YAML"),
    ],
)
def test_invalid_skill_metadata_is_rejected(text: str, problem: str) -> None:
    with pytest.raises(SkillError, match=problem):
        parse(text)


def test_semver_prerelease_and_build_metadata_are_accepted() -> None:
    parse(VALID.replace("1.2.3", "1.2.3-rc.1+build.5"))


# -- Discovery and precedence -----------------------------------------------------------------


def test_sources_resolve_in_precedence_order(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    found = skill_sources(["extra", "~/more", str(tmp_path / "abs")], project, tmp_path / "home")
    assert [(s.kind, s.root) for s in found[1:]] == [
        (SourceKind.GLOBAL, tmp_path / "home" / ".agent-cli" / "skills"),
        (SourceKind.PROJECT, project / ".agent-cli" / "skills"),
        (SourceKind.CONFIGURED, project / "extra"),
        (SourceKind.CONFIGURED, Path.home() / "more"),
        (SourceKind.CONFIGURED, tmp_path / "abs"),
    ]
    assert found[0].kind is SourceKind.BUILTIN


def test_later_sources_replace_whole_packages(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "repo"
    package(home / ".agent-cli" / "skills", "security", "Global security.\n", version="2.0.0")
    package(project / ".agent-cli" / "skills", "security", "Project security.\n", version="3.0.0")
    package(project / ".agent-cli" / "skills", "django", "Django rules.\n")
    package(tmp_path / "extra", "django", "Extra django.\n", description="Extra.")
    package(tmp_path / "more", "testing", "More testing.\n")

    catalog = load_catalog(sources(home, project, str(tmp_path / "extra"), "../more"))

    security = catalog["security"]
    assert (security.version, security.body, security.source.kind) == (
        "3.0.0",
        "Project security.\n",
        SourceKind.PROJECT,
    )
    django = catalog["django"]
    assert (django.description, django.body) == ("Extra.", "Extra django.\n")
    assert django.source.kind is SourceKind.CONFIGURED
    assert catalog["testing"].body == "More testing.\n"
    assert catalog["architecture"].source.kind is SourceKind.BUILTIN


def test_builtin_files_are_never_modified_by_overrides(tmp_path: Path) -> None:
    builtin = builtin_root() / "skills" / "security" / "SKILL.md"
    before = builtin.read_bytes()
    package(tmp_path / "home" / ".agent-cli" / "skills", "security", "Override.\n")
    load_catalog(sources(tmp_path / "home"))
    assert builtin.read_bytes() == before


def test_invalid_package_in_any_source_fails_discovery_naming_the_file(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".agent-cli" / "skills"
    path = package(root, "bad", extra="owner: me\n")
    with pytest.raises(SkillError, match=f"{path}: unknown metadata key"):
        load_catalog(sources(tmp_path / "home"))


def test_directory_name_mismatch_is_rejected(tmp_path: Path) -> None:
    package(tmp_path / "home" / ".agent-cli" / "skills", "security", directory="sec")
    with pytest.raises(SkillError, match="does not match its directory 'sec'"):
        load_catalog(sources(tmp_path / "home"))


def test_a_byte_order_mark_is_tolerated(tmp_path: Path) -> None:
    path = package(tmp_path / "skills", "windows")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    catalog = load_catalog([SkillSource(SourceKind.CONFIGURED, tmp_path / "skills")])
    assert catalog["windows"].body == "Rules.\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_unreadable_source_is_a_skill_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    package(root, "locked")
    root.chmod(0o000)
    try:
        with pytest.raises(SkillError, match=f"cannot read skill source {root}"):
            load_catalog([SkillSource(SourceKind.CONFIGURED, root)])
    finally:
        root.chmod(0o755)


def test_non_package_entries_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "notes").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "README.md").write_text("hi", encoding="utf-8")
    package(root, "real")
    found = read_source(SkillSource(SourceKind.CONFIGURED, root), Utf8ByteEstimator())
    assert [skill.name for skill in found] == ["real"]


def test_missing_configured_path_is_an_error_but_missing_defaults_are_not(tmp_path: Path) -> None:
    load_catalog(sources(tmp_path / "nowhere", tmp_path / "repo"))
    with pytest.raises(SkillError, match="does not exist"):
        load_catalog(sources(tmp_path, None, str(tmp_path / "missing")))


def test_catalog_reload_picks_up_changes_without_restart(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".agent-cli" / "skills"
    package(root, "django", "v1\n")
    assert load_catalog(sources(tmp_path / "home"))["django"].body == "v1\n"
    package(root, "django", "v2 with more text\n", version="1.1.0")
    package(root, "flask")
    reloaded = load_catalog(sources(tmp_path / "home"))
    assert reloaded["django"].body == "v2 with more text\n"
    assert reloaded["django"].version == "1.1.0"
    assert "flask" in reloaded


# -- Token estimation -------------------------------------------------------------------------


def test_token_estimate_is_deterministic_conservative_and_versioned(tmp_path: Path) -> None:
    estimator = Utf8ByteEstimator()
    assert estimator.id == "utf8-bytes-div3@1"
    assert estimator.estimate("") == 0
    assert estimator.estimate("abcd") == 2
    assert estimator.estimate("é") == 1  # two UTF-8 bytes
    assert estimator.estimate("x" * 300) == 100

    root = tmp_path / "skills"
    package(root, "sized", "y" * 299 + "\n")
    catalog = load_catalog([SkillSource(SourceKind.CONFIGURED, root)], estimator)
    assert catalog["sized"].tokens == 100
    assert catalog.estimator_id == estimator.id


# -- Policy -----------------------------------------------------------------------------------


def policy_data() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(default_policy_path().read_text(encoding="utf-8"))
    return data


def test_policy_parses_vocabulary_derivations_and_stable_mapping_order() -> None:
    policy = load_policy()
    assert policy.version == "3"
    assert policy.dynamic_escalation is True
    assert {"http_api", "authentication", "migration", "ci_cd"} <= policy.vocabulary.signals
    assert list(policy.skills)[:3] == ["specification", "architecture", "research"]
    assert policy.order("specification") == 0
    assert policy.order("user-skill") == len(policy.skills)
    assert policy.skills_for("retry") == ("resilience", "observability")
    assert policy.categories["migration"].signals == ("migration", "schema")


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda d: d.update(extra=1), "unknown key"),
        (lambda d: d.pop("signals"), "missing key"),
        (lambda d: d["skills"]["data"].update(signals=["telepathy"]), "unknown signal"),
        (lambda d: d["skills"].update(Bad={"signals": ["database"]}), "invalid skill name"),
        (lambda d: d["role_defaults"].update(implementer="testing"), "list of names"),
        (lambda d: d["role_defaults"].update(implementer=["testing", "testing"]), "repeat"),
        (lambda d: d["signals"].append("unused_signal"), "mapped to no skill"),
        (
            lambda d: d["repository_signals"]["facts"]["database"].update(strength="huge"),
            "strong or weak",
        ),
        (
            lambda d: d["repository_signals"]["categories"].update(ci={"signals": ["nope"]}),
            "expected exactly signals and strength",
        ),
        (lambda d: d["skills"]["data"].update(priority=1), "exactly one key"),
        (lambda d: d.update(dynamic_escalation="yes"), "true or false"),
    ],
)
def test_malformed_policy_is_rejected(change: Any, problem: str) -> None:
    data = policy_data()
    change(data)
    with pytest.raises(SkillError, match=problem):
        parse_policy(data, "policy.yaml")


def test_policy_naming_a_skill_missing_from_the_catalog_is_rejected(tmp_path: Path) -> None:
    data = policy_data()
    data["role_defaults"]["implementer"] = ["software-engineering", "rust"]
    policy = parse_policy(data, "policy.yaml")
    with pytest.raises(SkillError, match="missing from the catalog: rust"):
        policy.check_catalog(load_catalog(sources(tmp_path)))
