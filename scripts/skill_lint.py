"""发布前校验（8 条规则）。

本模块只做判定，不改动被检查的仓库。`publish-skill` 与 `lint-skill`
调用同一套规则，避免"发布时拦不住、事后才发现"。
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_ASSET_THRESHOLD = 1024 * 1024
DEFAULT_REGISTRY_PATH = "~/.codex/skill-git-repos/ark-kb/registry/skills.json"
DEFAULT_CACHE_DIR = "~/.codex/skill-git-repos"
DEFAULT_AUDIT_DIR = "~/.codex/skill-git-audit"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# 规则 2：命中即拒。根目录的 .git 是仓库自身，不属于发布内容，跳过不报。
EXCLUDED_DIR_NAMES = {
    ".venv",
    "venv",
    ".git",
    ".omo",
    ".opencode",
    "build",
    "dist",
    "__pycache__",
    "secrets",
}
EXCLUDED_FILE_SUFFIXES = (".log", ".key", ".pem", ".egg-info")
EXCLUDED_FILE_NAMES = {"plan.md", ".env"}


@dataclass
class Violation:
    rule: str
    level: str
    message: str
    paths: tuple[str, ...] = ()
    drift: bool = False

    def to_dict(self) -> dict:
        data = {"rule": self.rule, "level": self.level, "message": self.message}
        if self.paths:
            data["paths"] = list(self.paths)
        if self.drift:
            data["drift"] = True
        return data


@dataclass
class LintReport:
    skill: str
    path: Path
    violations: list[Violation] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    audit_path: Optional[str] = None

    def add(self, rule: str, level: str, message: str, paths: Iterable[str] = (), drift: bool = False) -> None:
        self.violations.append(Violation(rule, level, message, tuple(paths), drift))

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.level == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.level == "warning"]

    @property
    def drift(self) -> list[Violation]:
        return [v for v in self.violations if v.drift]

    def exit_code(self, fail_on_error: bool = True) -> int:
        if not fail_on_error:
            return 0
        if any(v.level == "error" and not v.drift for v in self.violations):
            return 3
        if any(v.level == "error" and v.drift for v in self.violations):
            return 8
        return 0

    @property
    def verdict(self) -> str:
        return "pass" if not self.errors else "fail"

    def to_dict(self, fail_on_error: bool = True) -> dict:
        return {
            "schema_version": 1,
            "skill": self.skill,
            "path": str(self.path),
            "verdict": self.verdict,
            "exit_code": self.exit_code(fail_on_error),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "stats": self.stats,
            "violations": [v.to_dict() for v in self.violations],
            "audit_path": self.audit_path,
        }

    def lines(self) -> list[str]:
        out = [f"skill: {self.skill}", f"path: {self.path}"]
        if self.violations:
            for item in self.violations:
                out.append(f"{item.level.upper()} [{item.rule}] {item.message}")
                out.extend(f"    {path}" for path in item.paths[:10])
                if len(item.paths) > 10:
                    out.append(f"    ... (+{len(item.paths) - 10} more)")
        else:
            out.append("all 8 rules passed")
        out.append(f"verdict: {self.verdict} ({len(self.errors)} errors, {len(self.warnings)} warnings)")
        if self.audit_path:
            out.append(f"audit: {self.audit_path}")
        return out


def human_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit != "B" else f"{value} B"
        value /= 1024.0
    return f"{value:.0f} GiB"


def cache_root() -> Path:
    return Path(os.environ.get("SKILL_GIT_CACHE_DIR", DEFAULT_CACHE_DIR)).expanduser()


def default_registry_path() -> Path:
    return Path(os.environ.get("SKILL_GIT_REGISTRY", DEFAULT_REGISTRY_PATH)).expanduser()


def default_audit_dir() -> Path:
    return Path(os.environ.get("SKILL_GIT_AUDIT_DIR", DEFAULT_AUDIT_DIR)).expanduser()


def _is_excluded_dir(name: str) -> bool:
    low = name.casefold()
    return low in EXCLUDED_DIR_NAMES or low.endswith(".egg-info")


def _is_excluded_file(name: str) -> bool:
    low = name.casefold()
    return low in EXCLUDED_FILE_NAMES or low.endswith(EXCLUDED_FILE_SUFFIXES)


def _git_candidates(root: Path) -> Optional[list[str]]:
    """git 工作树里"会被提交"的文件（已跟踪 + 未忽略的未跟踪文件）。

    返回 None 表示不是 git 工作树（或没装 git），此时按磁盘内容判定。
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "-c", "-o", "--exclude-standard"],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def _nested_git_dirs(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        for name in list(dirnames):
            path = current / name
            if name == ".git":
                if current != root:
                    found.append(path.relative_to(root).as_posix() + "/")
                dirnames.remove(name)
            elif _is_excluded_dir(name):
                dirnames.remove(name)
    return found


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return total


def walk_tree(root: Path) -> tuple[list[tuple[str, int]], list[str], int]:
    """返回 (文件列表[(相对路径, 字节)], 命中排除项的相对路径, 命中内容的总字节)。

    - git 工作树：只判定"会被提交"的内容（已跟踪 + 未忽略的未跟踪），
      本地跑测试产生的 `__pycache__/` 之类的忽略文件不算违规。
    - 非 git 目录：按磁盘内容判定。
    - 根目录的 .git 属于仓库自身，不对内容判定、也不计入体积。
    - 被排除内容的体积仍计入总量，否则"提交了 .venv"就不会触发体积门禁。
    """
    files: list[tuple[str, int]] = []
    hits: list[str] = []
    total = 0
    candidates = _git_candidates(root)
    if candidates is not None:
        seen_prefixes: set[str] = set()
        for relative in candidates:
            parts = Path(relative).parts
            path = root / relative
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total += size
            excluded_prefix = None
            for index, part in enumerate(parts[:-1]):
                if _is_excluded_dir(part):
                    excluded_prefix = "/".join(parts[: index + 1]) + "/"
                    break
            if excluded_prefix is not None:
                if excluded_prefix not in seen_prefixes:
                    seen_prefixes.add(excluded_prefix)
                    hits.append(excluded_prefix)
                continue
            if _is_excluded_file(parts[-1]):
                hits.append(relative)
                continue
            files.append((relative, size))
        hits.extend(_nested_git_dirs(root))
        return files, hits, total

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        relative_dir = current.relative_to(root)
        at_root = relative_dir.parts == ()
        for name in sorted(dirnames):
            if at_root and name == ".git":
                dirnames.remove(name)
                continue
            if _is_excluded_dir(name):
                relative = (relative_dir / name).as_posix()
                hits.append(relative + "/")
                dirnames.remove(name)
                total += _tree_bytes(current / name)
        for name in sorted(filenames):
            path = current / name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            relative = (relative_dir / name).as_posix()
            total += size
            if _is_excluded_file(name):
                hits.append(relative)
                continue
            files.append((relative, size))
    return files, hits, total


def load_registry(path: Path) -> tuple[dict[str, dict], Optional[str]]:
    """读取能力台账。返回 (name -> entry, 错误说明)。

    兼容三种形态：{"skills": [...]} / {"skills": {name: entry}} / 顶层列表。
    """
    if not path.is_file():
        return {}, f"registry not found: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"registry is unreadable: {path}: {exc}"
    entries: Iterable = ()
    if isinstance(data, dict):
        if isinstance(data.get("skills"), dict):
            entries = list(data["skills"].values())
        elif isinstance(data.get("skills"), list):
            entries = data["skills"]
        else:
            entries = [value for value in data.values() if isinstance(value, dict)]
    elif isinstance(data, list):
        entries = data
    else:
        return {}, f"registry has an unsupported shape: {path}"
    result: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name:
            result[name] = entry
    return result, None


def known_skill_names(root: Path, registry: dict[str, dict], extra: Iterable[str] = ()) -> set[str]:
    """内嵌副本判定用的已知 skill 名：台账 + 本地缓存仓库 + 自身 + 显式补充。"""
    names = {name for name in registry if name}
    names.add(root.name)
    names.update(str(item).strip() for item in extra if str(item).strip())
    try:
        for child in cache_root().iterdir():
            if child.is_dir() and not child.name.startswith("."):
                names.add(child.name)
    except OSError:
        pass
    return names


def _parse_meta(root: Path) -> tuple[Optional[dict], Optional[str]]:
    # 延迟导入避免与 skill_git 循环依赖（skill_git 在模块顶部导入本模块）。
    from skill_git import ValidationError, parse_skill

    if not (root / "SKILL.md").is_file():
        return None, "SKILL.md is missing at the repository root"
    try:
        return parse_skill(root), None
    except ValidationError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"SKILL.md is unreadable: {exc}"


def _check_frontmatter(report: LintReport, meta: Optional[dict], error: Optional[str], expected: str) -> None:
    if meta is None:
        report.add("frontmatter", "error", error or "invalid frontmatter")
        return
    name = str(meta.get("name", ""))
    if not NAME_PATTERN.fullmatch(name):
        report.add("frontmatter", "error", f"frontmatter name is not a legal skill name: {name!r}")
    elif name != expected:
        report.add(
            "frontmatter",
            "error",
            f"frontmatter name {name!r} does not match repository name {expected!r}",
        )
    if not str(meta.get("description", "")).strip():
        report.add("frontmatter", "error", "frontmatter description is empty")


def _check_excluded(report: LintReport, hits: Iterable[str]) -> None:
    hits = sorted(set(hits))
    if hits:
        report.add(
            "excluded",
            "error",
            f"{len(hits)} excluded path(s) are present; they must not be published",
            hits,
        )


def _check_size(report: LintReport, total: int, max_bytes: int, allow_large: bool) -> None:
    if total > max_bytes:
        message = f"repository is {human_bytes(total)}, above the {human_bytes(max_bytes)} limit"
        if allow_large:
            report.add("size", "warning", message + " (allowed by --allow-large)")
        else:
            report.add("size", "error", message + "; pass --allow-large to accept it explicitly")


def _check_embedded(report: LintReport, root: Path, known: set[str], files: Iterable[tuple[str, int]]) -> None:
    found: list[str] = []
    for relative, _ in files:
        parts = Path(relative).parts
        if Path(relative).name.casefold() != "skill.md" or len(parts) < 2:
            continue
        parent = Path(relative).parent.as_posix()
        found.append(parent + "/SKILL.md")
    # 没有 SKILL.md 的副本：目录名命中已知 skill 且带 skill 结构。
    children_with_skill_shape: dict[str, set[str]] = {}
    for relative, _ in files:
        parts = Path(relative).parts
        if len(parts) < 2:
            continue
        top = parts[0]
        if len(parts) >= 2 and parts[1] in {"scripts", "references"}:
            children_with_skill_shape.setdefault(top, set()).add(parts[1])
        if len(parts) == 2 and parts[1] in {"requirements.txt", "pyproject.toml"}:
            children_with_skill_shape.setdefault(top, set()).add(parts[1])
    for name, shape in sorted(children_with_skill_shape.items()):
        if name in known and shape:
            found.append(f"{name}/ (looks like a copy of the '{name}' skill)")
    if found:
        report.add(
            "embedded",
            "error",
            "embedded skill copies found; extract them into their own repository",
            sorted(set(found)),
        )


def _check_dependencies(report: LintReport, root: Path, files: Iterable[tuple[str, int]]) -> None:
    python_scripts = [rel for rel, _ in files if rel.startswith("scripts/") and rel.casefold().endswith(".py")]
    if not python_scripts:
        return
    declared = [name for name in ("requirements.txt", "pyproject.toml") if (root / name).is_file()]
    if not declared:
        report.add(
            "dependencies",
            "error",
            "scripts/ contains Python files but neither requirements.txt nor pyproject.toml is present",
            sorted(python_scripts)[:5],
        )


def summarize(items: Iterable[str], limit: int = 5) -> str:
    values = list(items)
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + f", ... (+{len(values) - limit} more)"


def _readme_layout(text: str) -> list[str]:
    entries: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip().casefold() == "## skill layout"
            continue
        stripped = line.strip()
        if in_section and stripped.startswith("- `") and stripped.endswith("`"):
            entries.append(stripped[3:-1])
    return entries


def _readme_tail(text: str, heading: str) -> str:
    """返回 heading 之后的全部内容（含其间的小标题）。

    生成的 README 以 usage 段结尾，而实际 README 可能在其后追加手写章节，
    因此正文比对只从生成侧取"到文件末尾"，实际侧用包含关系判断。
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() == heading.casefold():
            return "\n".join(lines[index + 1:])
    return ""


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def _check_readme(report: LintReport, root: Path, meta: Optional[dict], level: str, asset_threshold: int) -> None:
    readme = root / "README.md"
    if not readme.is_file():
        report.add(
            "readme",
            "warning",
            "README.md is missing; publish-skill generates it on the next publication",
        )
        return
    if meta is None:
        report.add("readme", "warning", "README consistency skipped: frontmatter is invalid")
        return
    try:
        text = readme.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.add("readme", "warning", f"README.md is unreadable: {exc}")
        return
    try:
        from skill_git import build_skill_readme, scan_skill
    except ImportError:  # pragma: no cover - 环境损坏时才可能发生
        report.add("readme", "warning", "README consistency skipped: skill_git is unavailable")
        return

    assets = {path.relative_to(root).as_posix() for path in scan_skill(root, asset_threshold).assets}
    canonical = build_skill_readme(meta, root, assets)
    if "## Skill layout" not in canonical.replace("\r\n", "\n"):
        report.add("readme", "warning", "README consistency skipped: the canonical README has no layout section")
        return
    normalized_readme = normalize_text(text)
    if "## Skill Layout".casefold() not in normalized_readme.casefold() and "## Install the skill".casefold() not in normalized_readme.casefold():
        report.add(
            "readme",
            "warning",
            "README.md is hand-written; rule 6 only compares generated READMEs",
        )
        return
    problems: list[str] = []

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    expected_title = f"# {meta['name']}"
    if first_line != expected_title:
        problems.append(f"title mismatch: expected {expected_title!r}, found {first_line[:80]!r}")
    if normalize_text(meta.get("description", "")) not in normalize_text(text):
        problems.append("description in SKILL.md is not present in README.md")

    expected_layout = _readme_layout(canonical)
    actual_layout = _readme_layout(text)
    missing = sorted(set(expected_layout) - set(actual_layout))
    extra = sorted(set(actual_layout) - set(expected_layout))
    if missing:
        problems.append(f"{len(missing)} layout entries missing from README.md: " + summarize(missing))
    if extra:
        problems.append(f"{len(extra)} layout entries not in the skill directory: " + summarize(extra))

    canonical_usage = normalize_text(_readme_tail(canonical, "## Usage and workflow"))
    if canonical_usage and canonical_usage not in normalize_text(text):
        problems.append("usage section drifted from the SKILL.md body; republish to regenerate README.md")

    for required in (f"~/.codex/skills/{meta['name']}", f"~/.config/opencode/skills/{meta['name']}"):
        if required not in text:
            report.add("readme", "warning", f"README.md install section does not mention {required}")

    if problems:
        report.add(
            "readme",
            level,
            "README.md drifted from SKILL.md: " + "; ".join(problems),
            ("README.md",),
            drift=True,
        )


def _check_registry(report: LintReport, name: str, registry: dict[str, dict], registry_error: Optional[str], path: Path) -> None:
    if registry_error:
        if registry_error.startswith("registry not found"):
            report.add(
                "registry",
                "warning",
                f"rule 7 skipped: {registry_error}; register the skill to enable the check",
            )
        else:
            report.add("registry", "error", f"rule 7 cannot be verified: {registry_error}")
        return
    entry = registry.get(name)
    if entry is None:
        report.add(
            "registry",
            "error",
            f"skill {name!r} is not registered in {path}",
            (str(path),),
        )
        return
    status = str(entry.get("status", "")).strip().casefold()
    if status == "archived":
        report.add("registry", "error", f"skill {name!r} is archived in the registry and must not be published")
    if not str(entry.get("reuse", "")).strip():
        report.add("registry", "error", f"registry entry for {name!r} has an empty reuse field")
    if not str(entry.get("scope_note", "")).strip():
        report.add("registry", "error", f"registry entry for {name!r} has an empty scope_note field")


def lint_skill(
    root: Path,
    *,
    expected_name: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_large: bool = False,
    registry_path: Optional[Path] = None,
    known_skills: Iterable[str] = (),
    readme_level: str = "error",
    asset_threshold: int = DEFAULT_ASSET_THRESHOLD,
) -> LintReport:
    root = Path(root).expanduser().resolve()
    expected = expected_name or root.name
    report = LintReport(skill=expected, path=root)
    registry_file = Path(registry_path).expanduser() if registry_path else default_registry_path()
    registry, registry_error = load_registry(registry_file)

    meta, meta_error = _parse_meta(root)
    name = str(meta.get("name", "")).strip() if meta else expected
    if meta:
        report.skill = name

    files, excluded_hits, total = walk_tree(root)

    _check_frontmatter(report, meta, meta_error, expected)
    _check_excluded(report, excluded_hits)
    _check_size(report, total, max_bytes, allow_large)
    _check_embedded(report, root, known_skill_names(root, registry, known_skills), files)
    _check_dependencies(report, root, files)
    _check_readme(report, root, meta, readme_level, asset_threshold)
    _check_registry(report, name, registry, registry_error, registry_file)

    report.stats = {
        "files_checked": len(files),
        "total_bytes": total,
        "max_bytes": max_bytes,
        "allow_large": allow_large,
        "registry": str(registry_file),
        "registry_state": "missing" if registry_error and registry_error.startswith("registry not found")
        else ("unreadable" if registry_error else f"{len(registry)} entries"),
        "asset_threshold": asset_threshold,
    }
    return report


def actor_identity() -> str:
    explicit = os.environ.get("SKILL_GIT_ACTOR", "").strip()
    if explicit:
        return explicit
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - 无 HOME 的极端环境
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def write_audit(
    report: LintReport,
    audit_dir: Optional[Path] = None,
    *,
    command: str = "lint-skill",
    fail_on_error: bool = True,
    extra: Optional[dict] = None,
) -> Path:
    directory = Path(audit_dir).expanduser() if audit_dir else default_audit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "command": command,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "actor": actor_identity(),
        "hostname": socket.gethostname(),
        "skill": report.skill,
        "path": str(report.path),
        "verdict": report.verdict,
        "exit_code": report.exit_code(fail_on_error),
        "stats": report.stats,
        "violations": [item.to_dict() for item in report.violations],
    }
    if extra:
        record.update(extra)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", report.skill or "skill")
    path = directory / f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{slug}.json"
    suffix = 2
    while path.exists():
        path = directory / f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{slug}-{suffix}.json"
        suffix += 1
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.audit_path = str(path)
    return path
