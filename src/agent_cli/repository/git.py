"""The files a repository index describes, identified by the Git blob ID of their current content.

Clean tracked files use the blob ID Git already has. Dirty tracked files and non-ignored
untracked files get the blob ID their current content would have (what ``git hash-object``
computes), so local edits are indexed without a commit and identical contents still share one
cached analysis. Ignored files, Git internals, and Agent CLI's own runtime state are excluded.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_cli.state.paths import AGENT_DIR

EXCLUDED_PARTS = frozenset({".git", AGENT_DIR})
SUBMODULE_MODE = "160000"
SYMLINK_MODE = "120000"


class RepositoryError(Exception):
    """The repository could not be read. No task or index state was changed."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    """Repository-relative POSIX path."""
    blob: str
    """Git blob ID of the content currently in the working tree."""


def blob_id(content: bytes, object_format: str = "sha1") -> str:
    """The Git blob object ID of ``content``, as ``git hash-object`` would compute it."""
    digest = hashlib.new(object_format)
    digest.update(b"blob %d\0" % len(content))
    digest.update(content)
    return digest.hexdigest()


def excluded(path: str) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.split("/"))


class GitWorkspace:
    """Read-only access to a Git working tree."""

    def __init__(self, root: Path) -> None:
        top = self._git_at(root, "rev-parse", "--show-toplevel").strip()
        self.root = Path(top)
        try:
            self.object_format = self._git("rev-parse", "--show-object-format").strip() or "sha1"
        except RepositoryError:
            self.object_format = "sha1"

    def _git(self, *args: str) -> str:
        return self._git_at(self.root, *args)

    @staticmethod
    def _git_at(root: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
            )
        except OSError as error:
            raise RepositoryError(f"cannot run git: {error.strerror or error}") from error
        if result.returncode != 0:
            message = result.stderr.strip() or f"git {args[0]} failed"
            raise RepositoryError(f"{root} is not a readable Git repository: {message}")
        return result.stdout

    def files(self) -> list[FileEntry]:
        """Tracked and non-ignored untracked regular files, with current-content blob IDs."""
        indexed: dict[str, str] = {}
        conflicted: set[str] = set()
        for record in split(self._git("ls-files", "-z", "--stage")):
            meta, _, path = record.partition("\t")
            mode, sha, stage = meta.split(" ")
            if mode in (SUBMODULE_MODE, SYMLINK_MODE) or excluded(path):
                continue
            if stage != "0":
                conflicted.add(path)
            indexed[path] = sha

        dirty = conflicted | set(split(self._git("diff-files", "-z", "--name-only")))
        untracked = [
            path
            for path in split(self._git("ls-files", "-z", "--others", "--exclude-standard"))
            if not excluded(path)
        ]

        entries = []
        for path, sha in indexed.items():
            if path not in dirty:
                entries.append(FileEntry(path, sha))
            elif (content := self.read(path)) is not None:
                entries.append(FileEntry(path, blob_id(content, self.object_format)))
        for path in untracked:
            if (content := self.read(path)) is not None:
                entries.append(FileEntry(path, blob_id(content, self.object_format)))
        return sorted(entries, key=lambda entry: entry.path)

    def read(self, path: str) -> bytes | None:
        """The bytes of a working-tree file; ``None`` if it is not (or no longer) a regular file."""
        file = self.root / path
        try:
            if file.is_symlink() or not file.is_file():
                return None
            return file.read_bytes()
        except OSError:
            return None


def split(output: str) -> list[str]:
    return [item for item in output.split("\0") if item]
