"""Discover and select repository-aware operating skills before an agent run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "use",
    "when",
    "with",
}


def _words(value: str) -> set[str]:
    return {word for word in _WORD.findall(value.casefold()) if word not in _STOP_WORDS}


def _phrase(value: str) -> str:
    return " ".join(_WORD.findall(value.casefold()))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


@dataclass(frozen=True)
class Skill:
    """One fully loaded skill document and the metadata used to select it."""

    name: str
    description: str
    triggers: tuple[str, ...]
    path: Path
    content: str


@dataclass(frozen=True)
class SkillResolution:
    """Auditable result of one pre-run discovery and selection pass."""

    discovered: tuple[Skill, ...]
    selected: tuple[Skill, ...]
    missing_required: tuple[str, ...]


class SkillResolver:
    """Find nested ``SKILL.md`` files and select those relevant to an operation."""

    def __init__(self, roots: list[Path], *, max_auto_skills: int = 8) -> None:
        self.roots = roots
        self.max_auto_skills = max_auto_skills

    @staticmethod
    def _load(path: Path) -> Skill:
        name = path.parent.name
        description = ""
        triggers: list[str] = []
        with path.open(encoding="utf-8") as handle:
            lines = []
            for line in handle:
                lines.append(line)
                if len(lines) > 1 and line.strip() == "---":
                    break
                if len(lines) >= 100:
                    break
        if lines and lines[0].strip() == "---":
            in_triggers = False
            for line in lines[1:]:
                stripped = line.strip()
                if stripped == "---":
                    break
                if stripped.startswith("name:"):
                    name = _unquote(stripped.partition(":")[2]) or name
                    in_triggers = False
                elif stripped.startswith("description:"):
                    description = _unquote(stripped.partition(":")[2])
                    in_triggers = False
                elif stripped == "triggers:":
                    in_triggers = True
                elif in_triggers and stripped.startswith("-"):
                    trigger = _unquote(stripped[1:])
                    if trigger:
                        triggers.append(trigger)
                elif stripped and not stripped.startswith("#"):
                    in_triggers = False
        return Skill(name, description, tuple(triggers), path, "")

    def discover(self) -> tuple[Skill, ...]:
        """Discover skills recursively; an earlier root wins duplicate names."""
        skills: list[Skill] = []
        names: set[str] = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            for path in sorted(root.rglob("SKILL.md")):
                if not path.is_file():
                    continue
                try:
                    path.resolve().relative_to(resolved_root)
                except ValueError:
                    continue
                try:
                    skill = self._load(path)
                except (OSError, UnicodeError):
                    continue
                key = skill.name.casefold()
                if key in names:
                    continue
                names.add(key)
                skills.append(skill)
        return tuple(skills)

    @staticmethod
    def _score(skill: Skill, query: str, query_words: set[str]) -> int:
        normalized_query = _phrase(query)
        normalized_name = _phrase(skill.name)
        score = 80 if normalized_name and normalized_name in normalized_query else 0
        normalized_triggers = [_phrase(trigger) for trigger in skill.triggers]
        if any(trigger in normalized_query for trigger in normalized_triggers if trigger):
            score += 100
        score += len(_words(skill.name) & query_words) * 12
        score += len(_words(" ".join(skill.triggers)) & query_words) * 6
        score += len(_words(skill.description) & query_words) * 2
        return score

    def resolve(self, required: list[str], query: str) -> SkillResolution:
        """Load required skills first, followed by the strongest contextual matches."""
        discovered = self.discover()
        by_name = {skill.name.casefold(): skill for skill in discovered}
        selected: list[Skill] = []
        selected_names: set[str] = set()
        missing: list[str] = []
        for name in required:
            skill = by_name.get(name.casefold())
            if skill is None:
                missing.append(name)
                continue
            selected.append(skill)
            selected_names.add(skill.name.casefold())

        query_words = _words(query)
        ranked = sorted(
            (
                (self._score(skill, query, query_words), index, skill)
                for index, skill in enumerate(discovered)
                if skill.name.casefold() not in selected_names
            ),
            key=lambda item: (-item[0], item[1]),
        )
        for auto_loaded, (score, _, skill) in enumerate(ranked):
            if score < 12 or auto_loaded >= self.max_auto_skills:
                break
            selected.append(skill)
        loaded = tuple(
            Skill(
                skill.name,
                skill.description,
                skill.triggers,
                skill.path,
                skill.path.read_text(encoding="utf-8"),
            )
            for skill in selected
        )
        return SkillResolution(discovered, loaded, tuple(missing))
