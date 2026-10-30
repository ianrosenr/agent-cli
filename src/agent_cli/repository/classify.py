"""Deterministic path, manifest, and import classification.

Every classification carries the evidence it came from, so a wrong or missing fact can be
traced to a specific rule (an inspection miss) rather than blamed on a later model.

Bump ``CLASSIFIER_VERSION`` whenever a rule changes: the repository index is rebuilt for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

CLASSIFIER_VERSION = "1"

SOURCE = "source"
TEST = "test"
MIGRATION = "migration"
CI = "ci"
DEPLOYMENT = "deployment"
CONFIGURATION = "configuration"
DEPENDENCY_MANIFEST = "dependency_manifest"
DOCUMENTATION = "documentation"
GENERATED = "generated"
"""Generated or vendored content: classified by path, but neither analyzed nor searched."""

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".tf": "terraform",
}

MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "Gemfile",
        "Gemfile.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "composer.lock",
    }
)
MANIFEST_PATTERN = re.compile(r"^requirements([-_.][\w.-]+)?\.(txt|in)$")

CI_FILES = frozenset({".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml", ".travis.yml"})
CI_DIRS = (".github/workflows/", ".circleci/", ".buildkite/")

DEPLOYMENT_FILES = frozenset({"Procfile", "serverless.yml", "fly.toml", "app.yaml", "vercel.json"})
DEPLOYMENT_PATTERN = re.compile(r"^(Dockerfile.*|.*\.dockerfile|(docker-)?compose.*\.ya?ml)$")
DEPLOYMENT_PARTS = frozenset(
    {"deploy", "deployment", "deployments", "infra", "k8s", "kubernetes", "helm", "charts"}
)

TEST_PARTS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
TEST_PATTERN = re.compile(
    r"^(test_.+\.py|.+_test\.(py|go)|.+\.(test|spec)\.[jt]sx?|.+Tests?\.(java|kt|cs)|conftest\.py)$"
)

MIGRATION_PARTS = frozenset({"migrations", "migration", "migrate", "alembic"})
MIGRATION_PATTERN = re.compile(r"^\d{3,}[_-].+\.sql$")

CONFIG_SUFFIXES = frozenset({".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".conf", ".env"})
CONFIG_FILES = frozenset({".editorconfig", ".env.example", "Makefile", ".pre-commit-config.yaml"})

DOC_SUFFIXES = frozenset({".md", ".rst", ".adoc"})
"""Plain ``.txt`` is documentation only by name (``README.txt``) or location (``docs/``)."""
DOC_STEMS = frozenset({"README", "LICENSE", "CHANGELOG", "CONTRIBUTING", "NOTICE"})

GENERATED_PARTS = frozenset(
    {"node_modules", "vendor", "third_party", "dist", "build", ".venv", "venv", "__pycache__"}
)
GENERATED_PATTERN = re.compile(r"^(.+\.min\.(js|css)|.+_pb2(_grpc)?\.pyi?|.+\.generated\.\w+)$")

IMPORT_SIGNALS: dict[str, str] = {
    "fastapi": "http_api",
    "flask": "http_api",
    "django": "http_api",
    "starlette": "http_api",
    "aiohttp": "http_api",
    "requests": "http_client",
    "httpx": "http_client",
    "urllib3": "http_client",
    "sqlalchemy": "database",
    "sqlite3": "database",
    "psycopg": "database",
    "psycopg2": "database",
    "pymongo": "database",
    "redis": "cache_store",
    "alembic": "migration",
    "tenacity": "retries",
    "backoff": "retries",
    "jwt": "authentication",
    "passlib": "authentication",
    "authlib": "authentication",
    "subprocess": "process_execution",
    "asyncio": "concurrency",
    "threading": "concurrency",
}
"""Top-level imported modules that are cheap, deterministic evidence of what code touches."""


@dataclass(frozen=True, slots=True)
class Classification:
    fact: str
    """A category (``test``, ``migration``, ...), ``language:<name>``, or ``signal:<name>``."""
    evidence: str


def language_of(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def classify_path(path: str) -> list[Classification]:
    """Categories for one repository-relative path, each with the rule that produced it."""
    pure = PurePosixPath(path)
    name = pure.name
    directories = pure.parts[:-1]
    found: list[Classification] = []

    def add(fact: str, evidence: str) -> None:
        if all(existing.fact != fact for existing in found):
            found.append(Classification(fact, evidence))

    def directory_in(parts: frozenset[str]) -> str | None:
        return next((part for part in directories if part in parts), None)

    if (part := directory_in(GENERATED_PARTS)) is not None:
        add(GENERATED, f"path: inside '{part}/'")
    if GENERATED_PATTERN.match(name):
        add(GENERATED, f"path: generated file name '{name}'")
    if name in MANIFESTS or MANIFEST_PATTERN.match(name):
        add(DEPENDENCY_MANIFEST, f"path: dependency manifest '{name}'")
    if name in CI_FILES or any(path.startswith(prefix) for prefix in CI_DIRS):
        add(CI, f"path: CI configuration '{path}'")
    if name in DEPLOYMENT_FILES or DEPLOYMENT_PATTERN.match(name) or pure.suffix == ".tf":
        add(DEPLOYMENT, f"path: deployment file '{name}'")
    if (part := directory_in(DEPLOYMENT_PARTS)) is not None:
        add(DEPLOYMENT, f"path: inside '{part}/'")
    if (part := directory_in(MIGRATION_PARTS)) is not None:
        add(MIGRATION, f"path: inside '{part}/'")
    if MIGRATION_PATTERN.match(name):
        add(MIGRATION, f"path: numbered SQL migration '{name}'")
    if TEST_PATTERN.match(name):
        add(TEST, f"path: test file name '{name}'")
    if (part := directory_in(TEST_PARTS)) is not None:
        add(TEST, f"path: inside '{part}/'")
    if pure.suffix.lower() in CONFIG_SUFFIXES or name in CONFIG_FILES:
        add(CONFIGURATION, f"path: configuration file '{name}'")
    if pure.suffix.lower() in DOC_SUFFIXES or pure.stem.upper() in DOC_STEMS:
        add(DOCUMENTATION, f"path: documentation file '{name}'")
    if "docs" in directories or "doc" in directories:
        add(DOCUMENTATION, "path: inside 'docs/'")

    language = language_of(path)
    if language is not None:
        add(f"language:{language}", f"path: extension '{pure.suffix}'")
        if not any(c.fact in (TEST, GENERATED, MIGRATION) for c in found):
            add(SOURCE, f"path: {language} file")
    return found


def import_signals(module: str, line: int) -> list[Classification]:
    top = module.split(".")[0]
    signal = IMPORT_SIGNALS.get(top)
    if signal is None:
        return []
    return [Classification(f"signal:{signal}", f"imports '{module}' (line {line})")]
