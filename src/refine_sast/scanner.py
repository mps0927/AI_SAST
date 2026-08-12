from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .hashing import content_hash
from .models import FileRecord


CODE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
ASSET_EXTENSIONS = {".h264", ".hex", ".raw", ".dat", ".ttf", ".qasm", ".qinc", ".png", ".jpg"}


def detect_language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".c":
        return "c"
    if suffix in {".cc", ".cpp", ".cxx"}:
        return "cpp"
    if suffix == ".h":
        return "c-header"
    if suffix in {".hh", ".hpp", ".hxx"}:
        return "cpp-header"
    if suffix in {".s", ".asm"}:
        return "assembly"
    if PurePosixPath(path).name == "CMakeLists.txt" or suffix == ".cmake":
        return "cmake"
    if suffix in ASSET_EXTENSIONS:
        return "asset"
    if suffix in {".md", ".txt", ".1", ".3"}:
        return "documentation"
    return "other"


def classify_scope(path: str, language: str) -> tuple[str, str | None]:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    name = PurePosixPath(lowered).name
    suffix = PurePosixPath(lowered).suffix
    if language not in {"c", "cpp", "c-header", "cpp-header", "assembly"}:
        return "non-code", "not a C/C++/assembly source"
    if "build" in parts or "generated" in parts:
        return "generated", "generated/build path"
    if lowered.startswith("opensrc/"):
        return "bundled-opensource", "bundled third-party source"
    if lowered.startswith("vcfw/"):
        return "firmware-side", "firmware-side source"
    test_part = any(part in {"test", "tests", "test_apps", "testing"} for part in parts)
    test_name = bool(re.match(r"(?:test.*|.*_test(?:_.*)?)\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx)$", name))
    if test_part or test_name:
        return "test", "test source excluded as focal candidate"
    if "android" in parts:
        return "android-specific", "Android-specific profile"
    if (
        "hello_pi" in parts
        or "examples" in parts
        or "example" in parts
        or "demo" in parts
        or "demos" in parts
    ):
        return "example-demo", "example/demo excluded as focal candidate"
    if suffix in {".h", ".hh", ".hpp", ".hxx"}:
        return "header-context", "indexed as supporting context"
    if language == "assembly":
        return "primary-source", None
    return "primary-source", None


class RepositoryScanner:
    version = "scanner-v1"

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _git(self, *args: str) -> bytes:
        command = [
            "git",
            "-c",
            f"safe.directory={self.root.as_posix()}",
            "-C",
            str(self.root),
            *args,
        ]
        return subprocess.check_output(command)

    def commit(self) -> str:
        return self._git("rev-parse", "HEAD").decode("ascii").strip()

    def tracked_paths(self) -> list[str]:
        raw = self._git("ls-files", "-z")
        return sorted(item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item)

    def _build_memberships(self, tracked: set[str]) -> dict[str, list[str]]:
        memberships: dict[str, set[str]] = defaultdict(set)
        source_pattern = re.compile(r"(?P<path>[A-Za-z0-9_./${}-]+\.(?:c|cc|cpp|cxx|s|h))", re.IGNORECASE)
        for cmake_path in sorted(path for path in tracked if PurePosixPath(path).name == "CMakeLists.txt"):
            data = (self.root / Path(cmake_path)).read_text(encoding="utf-8", errors="replace")
            base = PurePosixPath(cmake_path).parent
            for match in source_pattern.finditer(data):
                token = match.group("path")
                if "$" in token:
                    continue
                candidate = str(base.joinpath(token)) if str(base) != "." else token
                normalized = str(PurePosixPath(candidate))
                if normalized in tracked:
                    memberships[normalized].add(cmake_path)
        return {key: sorted(value) for key, value in memberships.items()}

    def scan(self) -> tuple[list[FileRecord], str]:
        paths = self.tracked_paths()
        memberships = self._build_memberships(set(paths))
        records: list[FileRecord] = []
        for relative in paths:
            absolute = self.root / Path(relative)
            data = absolute.read_bytes()
            language = detect_language(relative)
            scope, reason = classify_scope(relative, language)
            lines = 0 if not data else data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            records.append(
                FileRecord(
                    path=relative,
                    language=language,
                    bytes=len(data),
                    physical_lines=lines,
                    content_hash=content_hash(data),
                    scope=scope,
                    build_memberships=memberships.get(relative, []),
                    skip_reason=reason,
                )
            )
        return records, self.commit()
