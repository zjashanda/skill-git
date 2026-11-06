from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

EXCLUDED_DIRS = {
    ".git",
    ".omo",
    ".opencode",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "logs",
    "multi_logs",
    "node_modules",
    "output",
    "result",
    "runtime",
    "secrets",
    "venv",
}
EXCLUDED_EXTENSIONS = {".log", ".pyc", ".pyo", ".tmp", ".key", ".pem"}
LOCAL_ONLY_FILES = {"plan.md", ".env", ".env.development", ".env.local", ".env.production"}


def is_local_only(filename: str) -> bool:
    value = filename.casefold()
    return value in LOCAL_ONLY_FILES or value.endswith(".local.json") or value.endswith(".secret.json")
GIT_WARN_BYTES = 1024 * 1024
GIT_RECOMMENDED_MAX = 5 * 1024 * 1024
GITHUB_HARD_BYTES = 100 * 1024 * 1024
TEXT_EXTENSIONS = {
    ".py", ".ps1", ".bat", ".cmd", ".sh", ".js", ".ts", ".json", ".jsonc",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".md", ".txt",
    ".xml", ".csv", ".sql", ".html",
}
SECRET_PATTERNS = (
    re.compile(r"LTAI[0-9A-Za-z]{12,}"),
    re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)
FOREGROUND_PATTERNS = (
    re.compile(r"Start-Process(?![^\r\n]*-WindowStyle\s+Hidden)", re.I),
    re.compile(r"cmd(?:\.exe)?\s+/c\s+start\b", re.I),
    re.compile(r"\bstart\s+[\"']?[A-Za-z]:\\", re.I),
    re.compile(r"os\.system\(\s*[\"']start\b", re.I),
)


@dataclass(frozen=True, slots=True)
class SkillScan:
    assets: tuple[Path, ...]
    excluded: tuple[Path, ...]
    secrets: tuple[str, ...]
    foreground_commands: tuple[str, ...]


def relative_paths(root: Path, paths: Iterable[Path]) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in paths)


def scan_skill(root: Path, threshold: int = GIT_WARN_BYTES) -> SkillScan:
    assets: list[Path] = []
    excluded: list[Path] = []
    secrets: list[str] = []
    foreground: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        parts = set(relative.parts)
        if parts & EXCLUDED_DIRS or path.suffix.lower() in EXCLUDED_EXTENSIONS or is_local_only(path.name):
            excluded.append(path)
            continue
        size = path.stat().st_size
        if size > threshold:
            assets.append(path)
        if size > 2 * 1024 * 1024 or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            with path.open("rb") as binary:
                sample = binary.read(2 * 1024 * 1024 + 1)
            if b"\x00" in sample:
                continue
        except OSError:
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    secrets.append(str(relative))
                    break
            if path.name not in {"skill_assets.py", "skill_git.py"} and any(pattern.search(text) for pattern in FOREGROUND_PATTERNS):
                foreground.append(str(relative))
    return SkillScan(tuple(assets), tuple(excluded), tuple(sorted(set(secrets))), tuple(sorted(set(foreground))))


ALLOWED_ROOT_FILES = {
    ".gitignore",
    "SKILL.md",
    "README.md",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "install.sh",
    "install_requirements.bat",
    "preflight.bat",
    "run_multi.bat",
    "run_powercycle.bat",
    "preflight_powercycle.bat",
}
ALLOWED_ROOT_DIRS = {
    "agents",
    "assets",
    "audio",
    "config",
    "data",
    "fixtures",
    "lstest",
    "references",
    "scripts",
    "tests",
    "tools",
    "wav",
}


def layout_warnings(root: Path) -> list[str]:
    warnings: list[str] = []
    files = [path for path in root.iterdir() if path.is_file()]
    dirs = [path for path in root.iterdir() if path.is_dir()]
    unknown_files = [
        path.name
        for path in files
        if path.name not in ALLOWED_ROOT_FILES and not is_local_only(path.name)
    ]
    unknown_dirs = [path.name for path in dirs if path.name not in ALLOWED_ROOT_DIRS]
    if unknown_files:
        warnings.append(f"root files not in the standard layout: {', '.join(sorted(unknown_files))}")
    if unknown_dirs:
        warnings.append(f"root directories not in the standard layout: {', '.join(sorted(unknown_dirs))}")
    if len(files) > 12:
        warnings.append(f"root has {len(files)} files; keep only entry/install files at the root")
    has_python = any(path.suffix == ".py" for path in root.rglob("*.py"))
    if has_python and not (root / "requirements.txt").is_file() and not (root / "pyproject.toml").is_file():
        warnings.append("Python scripts exist but requirements.txt/pyproject.toml is missing")
    skill_md = root / "SKILL.md"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        if "环境变量" not in text and "Environment" not in text:
            warnings.append("SKILL.md does not document environment requirements")
    return warnings


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(root: Path, assets: Iterable[Path], archive_path: Path) -> dict:
    entries = []
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in assets:
            relative = path.relative_to(root).as_posix()
            archive.write(path, relative)
            entries.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return {
        "assets": entries,
        "archive_size": archive_path.stat().st_size,
        "archive_sha256": sha256_file(archive_path),
    }


def load_oss():
    try:
        import alibabacloud_oss_v2 as oss
    except ImportError as exc:
        raise RuntimeError("install alibabacloud-oss-v2 from requirements.txt") from exc
    return oss


def public_url(bucket: str, endpoint: str, key: str) -> str:
    host = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{bucket}.{host}/{quote(key, safe='/')}"


def upload_archive(
    archive_path: Path,
    archive_name: str,
    prefix: str,
    bucket: str,
    endpoint: str,
    region: str,
    acl: str,
    manifest: dict,
    access_key_env: str = "ACCESS_KEY_ID",
    secret_key_env: str = "ACCESS_KEY_SECRET",
) -> dict:
    access_key = os.environ.get(access_key_env, "").strip()
    secret_key = os.environ.get(secret_key_env, "").strip()
    if not access_key or not secret_key:
        raise RuntimeError("ACCESS_KEY_ID and ACCESS_KEY_SECRET are required for OSS asset upload")
    oss = load_oss()
    provider = oss.credentials.StaticCredentialsProvider(access_key, secret_key)
    cfg = oss.config.load_default()
    cfg.credentials_provider = provider
    cfg.region = region
    cfg.endpoint = endpoint
    client = oss.Client(cfg)
    key = "/".join(part.strip("/") for part in (prefix, archive_name) if part.strip("/"))
    manifest_key = f"{key}.json"
    client.put_object_from_file(oss.PutObjectRequest(bucket=bucket, key=key, acl=acl), str(archive_path))
    result = {
        "schema_version": 1,
        "archive_key": key,
        "archive_url": public_url(bucket, endpoint, key),
        "archive_size": manifest["archive_size"],
        "archive_sha256": manifest["archive_sha256"],
        "assets": manifest["assets"],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        manifest_path = Path(handle.name)
    try:
        client.put_object_from_file(oss.PutObjectRequest(bucket=bucket, key=manifest_key, acl=acl), str(manifest_path))
    finally:
        manifest_path.unlink(missing_ok=True)
    result["manifest_key"] = manifest_key
    result["manifest_url"] = public_url(bucket, endpoint, manifest_key)
    return result


BOOTSTRAP_TEMPLATE = r'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "assets" / "asset-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    missing = [
        item for item in data["assets"]
        if not (ROOT / item["path"]).is_file()
    ]
    if not missing:
        print("all assets present")
        return 0
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=ROOT) as handle:
        archive = Path(handle.name)
    try:
        request = urllib.request.Request(data["archive_url"], headers={"User-Agent": "skill-assets/1"})
        with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if archive.stat().st_size != data["archive_size"] or sha256(archive) != data["archive_sha256"]:
            raise RuntimeError("asset archive checksum mismatch")
        with zipfile.ZipFile(archive) as bundle:
            for item in missing:
                member = bundle.getinfo(item["path"])
                destination = (ROOT / item["path"]).resolve()
                if ROOT.resolve() not in destination.parents:
                    raise RuntimeError(f"unsafe archive path: {item['path']}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, destination.open("wb") as target:
                    target.write(source.read())
                if sha256(destination) != item["sha256"]:
                    raise RuntimeError(f"asset checksum mismatch: {item['path']}")
        print("restored", len(missing), "assets")
        return 0
    finally:
        archive.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_bootstrap(repo_dir: Path, manifest: dict) -> None:
    assets_dir = repo_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "asset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    script = repo_dir / "scripts" / "ensure_assets.py"
    if not script.exists():
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(BOOTSTRAP_TEMPLATE, encoding="utf-8")
