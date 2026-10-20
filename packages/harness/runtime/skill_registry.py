"""Bounded, local-only discovery and reading of agent Skill documents."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re


MAX_SKILLS = 64
MAX_DESCRIPTION = 240
MAX_SKILL_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 256 * 1024
MAX_CATALOG_BYTES = 16 * 1024
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_MARKER = re.compile(r"(?<![\w$])\$([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![\w-])")


def default_skill_roots(runtime_file: Path | str) -> tuple[Path, Path]:
    """Resolve bundled Skills from source checkout or app Resources/runtime."""
    return (Path(runtime_file).resolve().parents[1] / "skills",
            Path.home() / "Library/Application Support/Generalist/skills")


@dataclass(frozen=True, slots=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    source: str
    digest: str


@dataclass(frozen=True, slots=True)
class SkillDocument:
    info: SkillInfo
    body: str


def _valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and len(name) <= 64


def _read_text(path: Path, limit: int) -> str:
    if not path.is_file():
        raise ValueError(f"not a file: {path.name}")
    if path.stat().st_size > limit:
        raise ValueError(f"file too large: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file too large: {path.name}")
    return raw.decode("utf-8")


def _parse(text: str) -> tuple[str, str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing YAML front matter")
    fields: dict[str, str] = {}
    end = None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = index
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0].isspace() or ":" not in line:
            raise ValueError("only scalar name and description are supported")
        key, value = line.split(":", 1)
        if key not in {"name", "description"} or key in fields:
            raise ValueError("only unique name and description fields are supported")
        value = value.strip()
        if not value or value[0] in "[{>|&*!":
            raise ValueError("only nonempty scalar metadata is supported")
        if value[0] in "\"'" and value[-1:] == value[:1]:
            value = value[1:-1]
        fields[key] = value
    if end is None:
        raise ValueError("unterminated YAML front matter")
    name, description = fields.get("name", ""), fields.get("description", "")
    if not _valid_name(name):
        raise ValueError("invalid Skill name")
    if not description or len(description) > MAX_DESCRIPTION:
        raise ValueError("invalid Skill description")
    return name, description, "".join(lines[end + 1:])


def parse_explicit_skill(message: str, known_names: Collection[str]) -> tuple[str, tuple[str, ...]]:
    """Remove exact known $name markers from model-facing task text."""
    selected: list[str] = []
    known = set(known_names)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in known:
            return match.group(0)
        if name not in selected:
            selected.append(name)
        return ""

    cleaned = _MARKER.sub(replace, message)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), tuple(selected)


class SkillRegistry:
    """Discover immediate child Skills from fixed, explicit roots only."""

    def __init__(self, bundled_root: Path, personal_root: Path, extra_roots: Sequence[Path] = ()) -> None:
        self.bundled_root = Path(bundled_root).expanduser().resolve()
        self.personal_root = Path(personal_root).expanduser().resolve()
        self.extra_roots = tuple(Path(root).expanduser() for root in extra_roots)
        if any(not root.is_absolute() for root in self.extra_roots):
            raise ValueError("skills_dirs entries must be absolute paths")
        self.extra_roots = tuple(root.resolve() for root in self.extra_roots)
        self.diagnostics: tuple[str, ...] = ()
        self._entries: dict[str, SkillDocument] = {}

    def discover(self) -> tuple[SkillInfo, ...]:
        documents: list[SkillDocument] = []
        diagnostics: list[str] = []
        roots = ((self.bundled_root, "bundled"), (self.personal_root, "personal")) + tuple(
            (root, str(root)) for root in self.extra_roots
        )
        for root, source in roots:
            if not root.exists():
                continue
            if not root.is_dir():
                diagnostics.append(f"{source}: Skill root is not a directory")
                continue
            try:
                children = sorted(root.iterdir())
            except OSError as exc:
                diagnostics.append(f"{source}: cannot list Skills ({exc.strerror})")
                continue
            for folder in children:
                if not folder.is_dir():
                    continue
                try:
                    resolved = folder.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        raise ValueError("Skill directory escapes its root")
                    document_path = (resolved / "SKILL.md").resolve(strict=True)
                    if not document_path.is_relative_to(resolved):
                        raise ValueError("SKILL.md escapes its Skill directory")
                    raw = _read_text(document_path, MAX_SKILL_BYTES)
                    name, description, body = _parse(raw)
                    if name != folder.name:
                        raise ValueError("name does not match directory")
                    info = SkillInfo(name, description, resolved, source, hashlib.sha256(raw.encode("utf-8")).hexdigest())
                    documents.append(SkillDocument(info, body))
                except (OSError, UnicodeError, ValueError) as exc:
                    diagnostics.append(f"{source}/{folder.name}: {exc}")
        counts = Counter(document.info.name for document in documents)
        for name, count in sorted(counts.items()):
            if count > 1:
                diagnostics.append(f"duplicate Skill name '{name}' across roots; all copies skipped")
        entries: dict[str, SkillDocument] = {}
        catalog_size = 0
        for document in sorted(documents, key=lambda item: item.info.name):
            info = document.info
            if counts[info.name] > 1:
                continue
            added = len(f"{info.name}: {info.description}\n".encode("utf-8"))
            if len(entries) >= MAX_SKILLS or catalog_size + added > MAX_CATALOG_BYTES:
                diagnostics.append(f"{info.source}/{info.name}: Skill catalog limit reached")
                continue
            entries[info.name] = document
            catalog_size += added
        self._entries = entries
        self.diagnostics = tuple(diagnostics)
        return tuple(document.info for document in entries.values())

    def load(self, name: str) -> SkillDocument:
        if not _valid_name(name):
            raise ValueError(f"invalid Skill name: {name}")
        document = self._entries.get(name)
        if document is None:
            raise ValueError(f"Skill '{name}' is unavailable; run /skills reload")
        # Revalidate on use: a file may change or become a symlink after discovery.
        path = (document.info.path / "SKILL.md").resolve(strict=True)
        if not path.is_relative_to(document.info.path):
            raise ValueError(f"Skill '{name}' now escapes its directory")
        raw = _read_text(path, MAX_SKILL_BYTES)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest != document.info.digest:
            raise ValueError(f"Skill '{name}' changed; run /skills reload")
        return document

    def read(self, name: str, relative_path: str) -> str:
        document = self.load(name)
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or relative_path in {"", "."}:
            raise ValueError("Skill reference must be a relative path within the Skill")
        try:
            path = (document.info.path / relative).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Skill reference unavailable: {relative_path}") from exc
        if not path.is_relative_to(document.info.path):
            raise ValueError("Skill reference escapes its directory")
        return _read_text(path, MAX_REFERENCE_BYTES)

    def create(self, name: str) -> Path:
        if not _valid_name(name):
            raise ValueError("Skill name must be lowercase kebab-case (up to 64 characters)")
        self.personal_root.mkdir(parents=True, exist_ok=True)
        target = self.personal_root / name
        target.mkdir(exist_ok=False)
        (target / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Describe when to use this workflow.\n---\n\n"
            "# Workflow\n\n## When to use\n\nDescribe the trigger.\n\n"
            "## Prerequisites\n\nList needed inputs and tools.\n\n"
            "## Steps\n\n1. Inspect the task and available tools.\n2. Perform the work.\n"
            "3. Verify the result.\n\n## Stop conditions\n\n"
            "Stop and ask the user if authorization or required input is missing.\n",
            encoding="utf-8",
        )
        return target
