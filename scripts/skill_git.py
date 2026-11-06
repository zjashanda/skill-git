#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import quote

from skill_assets import (
    EXCLUDED_DIRS,
    LOCAL_ONLY_FILES,
    build_archive,
    layout_warnings,
    scan_skill,
    upload_archive,
    write_bootstrap,
)

DEFAULT_OWNER = os.environ.get("SKILL_GIT_OWNER", "zjashanda")
DEFAULT_BRANCH = os.environ.get("SKILL_GIT_BRANCH", "main")
DEFAULT_SSH_HOST = os.environ.get("SKILL_GIT_SSH_HOST", "github-zjashanda")
DEFAULT_VISIBILITY = os.environ.get("SKILL_GIT_VISIBILITY", "public")
DEFAULT_TOKEN_ENV = os.environ.get("SKILL_GIT_TOKEN_ENV", "GITHUB_TOKEN")
DEFAULT_TRANSPORT = os.environ.get("SKILL_GIT_TRANSPORT", "auto")
DEFAULT_OSS_ACCESS_KEY_ENV = os.environ.get("SKILL_GIT_OSS_ACCESS_KEY_ENV", "ACCESS_KEY_ID")
DEFAULT_OSS_SECRET_ENV = os.environ.get("SKILL_GIT_OSS_SECRET_ENV", "ACCESS_KEY_SECRET")
DEFAULT_OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com")
DEFAULT_OSS_REGION = os.environ.get("OSS_REGION", "cn-beijing")
DEFAULT_OSS_BUCKET = os.environ.get("OSS_BUCKET", "listenai-test-internal")
DEFAULT_OSS_PREFIX = os.environ.get("OSS_PREFIX", "exe/bszheng/")
DEFAULT_OSS_ACL = os.environ.get("OSS_ACL", "public-read")
DEFAULT_ASSET_MAX_BYTES = 1024 * 1024
DEFAULT_ASSET_THRESHOLD = int(os.environ.get("SKILL_GIT_ASSET_THRESHOLD", str(DEFAULT_ASSET_MAX_BYTES)))
DEFAULT_ASSET_MODE = os.environ.get("SKILL_GIT_ASSET_MODE", "auto")
API_BASE = "https://api.github.com"


def run_command(
    command,
    cwd: Optional[Path] = None,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_env,
        check=False,
    )
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(details)
    return result


def windows_env_value(name: str) -> Tuple[Optional[str], Optional[str]]:
    if os.name != "nt":
        return None, None
    try:
        import winreg
    except ImportError:
        return None, None

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment", "user-env-registry"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            "machine-env-registry",
        ),
    )
    for hive, subkey, source in locations:
        try:
            with winreg.OpenKey(hive, subkey) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
        except OSError:
            continue
        if value:
            return str(value), source
    return None, None


def resolve_token(token_env: str) -> Tuple[str, str]:
    token = os.environ.get(token_env)
    if token:
        return token, "process-env"

    token, source = windows_env_value(token_env)
    if token:
        return token, source or "windows-registry"

    raise RuntimeError(
        f"Token {token_env} was not found in the current process or Windows environment registry."
    )


def resolve_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} was not found in the current process.")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_oss_sdk():
    try:
        import alibabacloud_oss_v2 as oss
    except ImportError as exc:
        raise RuntimeError(
            "OSS upload requires alibabacloud-oss-v2. Install it with: "
            "python -m pip install -r requirements.txt"
        ) from exc
    return oss


def resolve_asset_path(skill_path: Path, value: str) -> Path:
    candidate = (skill_path / value).resolve()
    try:
        candidate.relative_to(skill_path.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Asset path escapes skill root: {value}") from exc
    return candidate


def collect_asset_files(skill_path: Path, requested: list[str], include_large: bool, max_bytes: int) -> list[Path]:
    paths: set[Path] = set()
    for value in requested:
        path = resolve_asset_path(skill_path, value)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            paths.add(path)
        else:
            paths.update(
                child
                for child in path.rglob("*")
                if child.is_file() and ".git" not in child.parts and "__pycache__" not in child.parts
            )

    if include_large:
        excluded = {".git", ".opencode", "result", "multi_logs", "logs", "__pycache__"}
        paths.update(
            child
            for child in skill_path.rglob("*")
            if child.is_file()
            and child.stat().st_size > max_bytes
            and not any(part in excluded for part in child.parts)
        )

    if not paths:
        raise RuntimeError("No asset files selected. Pass --asset or --include-large.")
    return sorted(paths)


def build_oss_client(oss, access_key_env: str, secret_key_env: str, region: str, endpoint: str):
    provider = oss.credentials.StaticCredentialsProvider(
        resolve_required_env(access_key_env),
        resolve_required_env(secret_key_env),
    )
    config = oss.config.load_default()
    config.credentials_provider = provider
    config.region = region
    config.endpoint = endpoint
    return oss.Client(config)


def oss_public_url(bucket: str, endpoint: str, key: str) -> str:
    host = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{bucket}.{host}/{quote(key, safe='/') }"


def run_upload_assets(args: argparse.Namespace) -> int:
    skill_path = resolve_skill_path(args.skill_name, args.skill_path)
    repo_name = parse_skill(skill_path)["name"]
    assets = collect_asset_files(skill_path, args.asset, args.include_large, args.max_bytes)
    oss = load_oss_sdk()
    client = build_oss_client(
        oss,
        args.oss_access_key_env,
        args.oss_secret_key_env,
        args.oss_region,
        args.oss_endpoint,
    )
    prefix = args.oss_prefix.strip("/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = args.archive_name or f"{repo_name}-assets-{stamp}.zip"
    archive_key = "/".join(part for part in (prefix, archive_name) if part)
    manifest_key = f"{archive_key}.json"

    with tempfile.TemporaryDirectory(prefix="skill-git-assets-") as temporary:
        archive_path = Path(temporary) / archive_name
        entries: list[dict[str, Any]] = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in assets:
                relative = path.relative_to(skill_path).as_posix()
                archive.write(path, relative)
                entries.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})

        archive_sha256 = sha256_file(archive_path)
        request = oss.PutObjectRequest(bucket=args.oss_bucket, key=archive_key, acl=args.oss_acl)
        client.put_object_from_file(request, str(archive_path))
        manifest = {
            "schema_version": 1,
            "skill": repo_name,
            "archive_key": archive_key,
            "archive_url": oss_public_url(args.oss_bucket, args.oss_endpoint, archive_key),
            "archive_size": archive_path.stat().st_size,
            "archive_sha256": archive_sha256,
            "assets": entries,
        }
        manifest_path = Path(temporary) / f"{archive_name}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        client.put_object_from_file(
            oss.PutObjectRequest(bucket=args.oss_bucket, key=manifest_key, acl=args.oss_acl),
            str(manifest_path),
        )

    print(json.dumps({**manifest, "manifest_key": manifest_key, "manifest_url": oss_public_url(args.oss_bucket, args.oss_endpoint, manifest_key)}, ensure_ascii=False, indent=2))
    return 0


def github_request(
    method: str,
    path: str,
    token: str,
    payload: Optional[Dict[str, object]] = None,
) -> Tuple[int, Dict[str, object]]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request) as response:
            payload_text = response.read().decode("utf-8")
            return response.status, json.loads(payload_text) if payload_text else {}
    except urllib.error.HTTPError as exc:
        payload_text = exc.read().decode("utf-8", errors="replace")
        body = json.loads(payload_text) if payload_text else {}
        return exc.code, body


def repo_exists(owner: str, repo_name: str, token: str) -> bool:
    status, _ = github_request("GET", f"/repos/{owner}/{repo_name}", token)
    if status == 200:
        return True
    if status == 404:
        return False
    raise RuntimeError(f"Unexpected GitHub status while checking repo: {status}")


def ensure_remote_repo(owner: str, repo_name: str, visibility: str, token: str) -> bool:
    if repo_exists(owner, repo_name, token):
        return False

    payload = {
        "name": repo_name,
        "private": visibility == "private",
        "description": f"Codex skill repository for {repo_name}.",
        "auto_init": False,
    }
    status, body = github_request("POST", "/user/repos", token, payload)
    if status not in (201, 202):
        message = body.get("message") if isinstance(body, dict) else body
        raise RuntimeError(f"Failed to create {owner}/{repo_name}: {message}")
    return True


def parse_skill(skill_path: Path) -> Dict[str, str]:
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(skill_md)

    raw = skill_md.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", raw, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"Missing valid frontmatter in {skill_md}")

    frontmatter: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()

    body = match.group(2).lstrip()
    title_match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else frontmatter.get("name", skill_path.name)
    return {
        "name": frontmatter.get("name", skill_path.name),
        "description": frontmatter.get("description", "").strip(),
        "body": body,
        "title": title,
    }


def strip_first_heading(body: str) -> str:
    return re.sub(r"\A#\s+.+?\r?\n+", "", body, count=1, flags=re.DOTALL).strip()


def build_skill_readme(meta: Dict[str, str], skill_path: Path, excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    files = [
        str(path.relative_to(skill_path)).replace("\\", "/")
        for path in sorted(skill_path.rglob("*"))
        if path.is_file()
        and path.name != "README.md"
        and path.name not in LOCAL_ONLY_FILES
        and str(path.relative_to(skill_path)).replace("\\", "/") not in excluded
        and "__pycache__" not in path.parts
        and not path.name.endswith(".pyc")
    ]

    lines = [
        f"# {meta['name']}",
        "",
        meta["description"],
        "",
        "## Skill layout",
        "",
    ]
    lines.extend(f"- `{item}`" for item in files)
    lines.extend(
        [
            "",
            "## Install the skill",
            "",
            "Copy this folder into:",
            "",
            "```text",
            f"~/.codex/skills/{meta['name']}",
            "```",
            "",
            "Then restart Codex.",
            "",
            "## Usage and workflow",
            "",
            strip_first_heading(meta["body"]),
            "",
        ]
    )
    return "\n".join(lines)


def should_skip(name: str) -> bool:
    return name in {".git", "__pycache__", ".DS_Store"} or name.endswith(".pyc")


def remove_worktree_contents(repo_dir: Path) -> None:
    for child in repo_dir.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_skill_to_repo_root(skill_path: Path, repo_dir: Path, excluded: set[str] | None = None) -> None:
    excluded = excluded or set()

    def copy_tree(source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            relative = child.relative_to(skill_path).as_posix()
            if (
                should_skip(child.name)
                or child.name in EXCLUDED_DIRS
                or child.name in LOCAL_ONLY_FILES
                or relative in excluded
            ):
                continue
            destination = target / child.name
            if child.is_dir():
                copy_tree(child, destination)
            elif child.name != "README.md":
                shutil.copy2(child, destination)

    copy_tree(skill_path, repo_dir)
    for pycache in repo_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    for pyc in repo_dir.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def remote_url(owner: str, repo_name: str, ssh_host: str) -> str:
    return f"git@{ssh_host}:{owner}/{repo_name}.git"


def https_remote_url(owner: str, repo_name: str) -> str:
    return f"https://github.com/{owner}/{repo_name}.git"


def ssh_probe(owner: str, repo_name: str, ssh_host: str) -> bool:
    remote = remote_url(owner, repo_name, ssh_host)
    result = run_command(
        ["git", "ls-remote", remote, "HEAD"],
        env={"GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=5"},
        check=False,
    )
    return result.returncode == 0


def select_transport(owner: str, repo_name: str, ssh_host: str, requested: str) -> str:
    if requested not in {"auto", "ssh", "https"}:
        raise RuntimeError(f"Unsupported Git transport: {requested}")
    if requested != "auto":
        return requested
    return "ssh" if ssh_probe(owner, repo_name, ssh_host) else "https"


@contextmanager
def git_auth_env(transport: str, token: str) -> Iterator[Dict[str, str]]:
    if transport != "https":
        yield {}
        return

    with tempfile.TemporaryDirectory(prefix="skill-git-askpass-") as temp_dir:
        askpass = Path(temp_dir) / "askpass.cmd"
        askpass.write_text(
            "@echo off\r\n"
            '"%SKILL_GIT_PYTHON%" -c "import os,sys; prompt=sys.argv[1] if len(sys.argv)>1 else \'\'; print(\'x-access-token\' if \'Username\' in prompt else os.environ[\'SKILL_GIT_ASKPASS_TOKEN\'])" "%~1"\r\n',
            encoding="ascii",
        )
        yield {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "SKILL_GIT_ASKPASS_TOKEN": token,
            "SKILL_GIT_PYTHON": sys.executable,
        }


def default_repo_dir(repo_name: str) -> Path:
    return Path.home() / ".codex" / "skill-git-repos" / repo_name


def remote_branch_exists(repo_dir: Path, branch: str, env: Optional[Dict[str, str]] = None) -> bool:
    result = run_command(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir,
        env=env,
        check=False,
    )
    return bool((result.stdout or "").strip())


def ensure_git_identity(repo_dir: Path, owner: str) -> None:
    current_name = run_command(
        ["git", "config", "--get", "user.name"],
        cwd=repo_dir,
        check=False,
    ).stdout.strip()
    current_email = run_command(
        ["git", "config", "--get", "user.email"],
        cwd=repo_dir,
        check=False,
    ).stdout.strip()

    default_name = os.environ.get("SKILL_GIT_GIT_USER_NAME") or current_name or owner
    default_email = (
        os.environ.get("SKILL_GIT_GIT_USER_EMAIL")
        or current_email
        or f"{owner}@users.noreply.github.com"
    )
    run_command(["git", "config", "user.name", default_name], cwd=repo_dir)
    run_command(["git", "config", "user.email", default_email], cwd=repo_dir)


def ensure_local_repo(
    repo_dir: Path,
    owner: str,
    repo_name: str,
    branch: str,
    ssh_host: str,
    transport: str,
    token: str,
) -> None:
    remote = remote_url(owner, repo_name, ssh_host) if transport == "ssh" else https_remote_url(owner, repo_name)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    with git_auth_env(transport, token) as auth:
        if not (repo_dir / ".git").exists():
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            run_command(["git", "clone", remote, str(repo_dir)], env=auth)

        run_command(["git", "remote", "set-url", "origin", remote], cwd=repo_dir, env=auth)
        run_command(["git", "fetch", "origin", "--prune"], cwd=repo_dir, env=auth, check=False)

        if remote_branch_exists(repo_dir, branch, env=auth):
            run_command(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, env=auth)
            run_command(["git", "pull", "--ff-only", "origin", branch], cwd=repo_dir, env=auth, check=False)
        else:
            run_command(["git", "checkout", "-B", branch], cwd=repo_dir, env=auth)

    ensure_git_identity(repo_dir, owner)


def repo_has_changes(repo_dir: Path) -> bool:
    result = run_command(["git", "status", "--porcelain"], cwd=repo_dir)
    return bool(result.stdout.strip())


def resolve_skill_path(skill_name: Optional[str], skill_path: Optional[str]) -> Path:
    if skill_path:
        path = Path(skill_path).expanduser().resolve()
    elif skill_name:
        path = (Path.home() / ".codex" / "skills" / skill_name).resolve()
    else:
        raise RuntimeError("Provide --skill-name or --skill-path.")

    if not path.exists():
        raise FileNotFoundError(path)
    return path


def resolve_repo_dir(repo_name: str, repo_dir: Optional[str]) -> Path:
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    return default_repo_dir(repo_name)


def commit_and_push(
    repo_dir: Path,
    branch: str,
    message: str,
    push: bool,
    transport: str,
    token: str,
) -> bool:
    if not repo_has_changes(repo_dir):
        return False

    run_command(["git", "add", "."], cwd=repo_dir)
    run_command(["git", "commit", "-m", message], cwd=repo_dir)
    if push:
        with git_auth_env(transport, token) as auth:
            run_command(["git", "push", "-u", "origin", branch], cwd=repo_dir, env=auth)
    return True


def run_show_config(args: argparse.Namespace) -> int:
    token, source = resolve_token(args.token_env)
    data = {
        "owner": args.owner,
        "branch": args.branch,
        "ssh_host": args.ssh_host,
        "visibility": args.visibility,
        "token_env": args.token_env,
        "token_source": source,
        "token_present": bool(token),
        "repo_rule": "repo name = skill name",
        "layout_rule": "repo root = skill contents",
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def run_ensure_repo(args: argparse.Namespace) -> int:
    token, source = resolve_token(args.token_env)
    created = ensure_remote_repo(args.owner, args.repo_name, args.visibility, token)
    repo_dir = resolve_repo_dir(args.repo_name, args.repo_dir)
    transport = select_transport(args.owner, args.repo_name, args.ssh_host, args.transport)
    ensure_local_repo(repo_dir, args.owner, args.repo_name, args.branch, args.ssh_host, transport, token)
    print(f"Repo ready: {args.owner}/{args.repo_name}")
    print(f"Token source: {source}")
    print(f"Git transport: {transport}")
    print(f"Local cache: {repo_dir}")
    print(f"Created remote repo: {'yes' if created else 'no'}")
    return 0


def run_publish_skill(args: argparse.Namespace) -> int:
    skill_path = resolve_skill_path(args.skill_name, args.skill_path)
    meta = parse_skill(skill_path)
    repo_name = meta["name"]
    scan = scan_skill(skill_path, args.asset_threshold)
    if scan.secrets:
        raise RuntimeError(f"refusing to publish secrets in: {', '.join(scan.secrets)}")
    if scan.foreground_commands:
        raise RuntimeError(f"foreground terminal command patterns found in: {', '.join(scan.foreground_commands)}")
    for warning in layout_warnings(skill_path):
        print(f"WARNING: {warning}")

    token, source = resolve_token(args.token_env)
    asset_manifest = None
    excluded: set[str] = set()
    if scan.assets:
        if args.asset_mode == "off":
            raise RuntimeError(
                f"{len(scan.assets)} file(s) exceed {args.asset_threshold} bytes; "
                "use --asset-mode auto or move them to OSS first"
            )
        with tempfile.TemporaryDirectory(prefix="skill-git-assets-") as temporary:
            archive_path = Path(temporary) / f"{repo_name}-assets.zip"
            archive = build_archive(skill_path, scan.assets, archive_path)
            asset_manifest = upload_archive(
                archive_path,
                f"{repo_name}-assets-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip",
                args.oss_prefix,
                args.oss_bucket,
                args.oss_endpoint,
                args.oss_region,
                args.oss_acl,
                archive,
                access_key_env=args.oss_access_key_env,
                secret_key_env=args.oss_secret_key_env,
            )
        asset_manifest["skill"] = repo_name
        excluded = {path.relative_to(skill_path).as_posix() for path in scan.assets}

    created = ensure_remote_repo(args.owner, repo_name, args.visibility, token)
    repo_dir = resolve_repo_dir(repo_name, args.repo_dir)
    transport = select_transport(args.owner, repo_name, args.ssh_host, args.transport)
    ensure_local_repo(repo_dir, args.owner, repo_name, args.branch, args.ssh_host, transport, token)

    readme_text = (
        Path(args.readme_file).expanduser().resolve().read_text(encoding="utf-8")
        if args.readme_file
        else build_skill_readme(meta, skill_path, excluded)
    )

    remove_worktree_contents(repo_dir)
    copy_skill_to_repo_root(skill_path, repo_dir, excluded)
    if asset_manifest is not None:
        write_bootstrap(repo_dir, asset_manifest)
    (repo_dir / "README.md").write_text(readme_text, encoding="utf-8")

    published_files = sum(1 for path in repo_dir.rglob("*") if path.is_file() and ".git" not in path.parts)
    asset_text = f", {len(scan.assets)} OSS assets" if scan.assets else ""
    commit_message = args.commit_message or f"Publish {repo_name}: {published_files} files{asset_text}"
    changed = commit_and_push(
        repo_dir,
        args.branch,
        commit_message,
        push=not args.no_push,
        transport=transport,
        token=token,
    )

    print(f"Skill: {repo_name}")
    print(f"Remote repo: {args.owner}/{repo_name}")
    print(f"Token source: {source}")
    print(f"Git transport: {transport}")
    print(f"Local cache: {repo_dir}")
    print(f"Created remote repo: {'yes' if created else 'no'}")
    print(f"Changed: {'yes' if changed else 'no'}")
    print(f"Pushed: {'no' if args.no_push else 'yes' if changed else 'no changes'}")
    if asset_manifest is not None:
        print(f"OSS archive: {asset_manifest['archive_url']}")
        print(f"OSS manifest: {asset_manifest['manifest_url']}")
    return 0
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish Codex skills to same-name GitHub repositories."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--owner", default=DEFAULT_OWNER)
    common.add_argument("--branch", default=DEFAULT_BRANCH)
    common.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    common.add_argument("--transport", choices=("auto", "ssh", "https"), default=DEFAULT_TRANSPORT)
    common.add_argument("--visibility", choices=("public", "private"), default=DEFAULT_VISIBILITY)
    common.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    common.add_argument("--repo-dir")

    show_parser = subparsers.add_parser("show-config", parents=[common], help="Show resolved token and repo defaults")
    show_parser.set_defaults(func=run_show_config)

    ensure_parser = subparsers.add_parser("ensure-repo", parents=[common], help="Ensure one same-name GitHub repo exists")
    ensure_parser.add_argument("--repo-name", required=True)
    ensure_parser.set_defaults(func=run_ensure_repo)

    publish_parser = subparsers.add_parser(
        "publish-skill",
        parents=[common],
        help="Publish one local skill into a same-name GitHub repository",
    )
    publish_parser.add_argument("--skill-name")
    publish_parser.add_argument("--skill-path")
    publish_parser.add_argument("--readme-file")
    publish_parser.add_argument("--commit-message")
    publish_parser.add_argument("--no-push", action="store_true")
    publish_parser.add_argument("--asset-mode", choices=("auto", "off"), default=DEFAULT_ASSET_MODE)
    publish_parser.add_argument("--asset-threshold", type=int, default=DEFAULT_ASSET_THRESHOLD)
    publish_parser.add_argument("--oss-access-key-env", default=DEFAULT_OSS_ACCESS_KEY_ENV)
    publish_parser.add_argument("--oss-secret-key-env", default=DEFAULT_OSS_SECRET_ENV)
    publish_parser.add_argument("--oss-endpoint", default=DEFAULT_OSS_ENDPOINT)
    publish_parser.add_argument("--oss-region", default=DEFAULT_OSS_REGION)
    publish_parser.add_argument("--oss-bucket", default=DEFAULT_OSS_BUCKET)
    publish_parser.add_argument("--oss-prefix", default=DEFAULT_OSS_PREFIX)
    publish_parser.add_argument("--oss-acl", choices=("private", "public-read", "public-read-write"), default=DEFAULT_OSS_ACL)
    publish_parser.set_defaults(func=run_publish_skill)

    upload_parser = subparsers.add_parser(
        "upload-assets",
        parents=[common],
        help="Package selected large skill assets and upload them to Alibaba Cloud OSS",
    )
    upload_parser.add_argument("--skill-name")
    upload_parser.add_argument("--skill-path")
    upload_parser.add_argument("--asset", action="append", default=[], help="Skill-relative file or directory; repeatable")
    upload_parser.add_argument("--include-large", action="store_true", help="Include files larger than --max-bytes, excluding runtime artifacts")
    upload_parser.add_argument("--max-bytes", type=int, default=1024 * 1024)
    upload_parser.add_argument("--archive-name", default="")
    upload_parser.add_argument("--oss-access-key-env", default=DEFAULT_OSS_ACCESS_KEY_ENV)
    upload_parser.add_argument("--oss-secret-key-env", default=DEFAULT_OSS_SECRET_ENV)
    upload_parser.add_argument("--oss-endpoint", default=DEFAULT_OSS_ENDPOINT)
    upload_parser.add_argument("--oss-region", default=DEFAULT_OSS_REGION)
    upload_parser.add_argument("--oss-bucket", default=DEFAULT_OSS_BUCKET)
    upload_parser.add_argument("--oss-prefix", default=DEFAULT_OSS_PREFIX)
    upload_parser.add_argument("--oss-acl", choices=("private", "public-read", "public-read-write"), default=DEFAULT_OSS_ACL)
    upload_parser.set_defaults(func=run_upload_assets)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
