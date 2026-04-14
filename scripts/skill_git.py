#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

DEFAULT_OWNER = os.environ.get("SKILL_GIT_OWNER", "zjashanda")
DEFAULT_BRANCH = os.environ.get("SKILL_GIT_BRANCH", "main")
DEFAULT_SSH_HOST = os.environ.get("SKILL_GIT_SSH_HOST", "github-zjashanda")
DEFAULT_VISIBILITY = os.environ.get("SKILL_GIT_VISIBILITY", "public")
DEFAULT_TOKEN_ENV = os.environ.get("SKILL_GIT_TOKEN_ENV", "GITHUB_TOKEN")
API_BASE = "https://api.github.com"


def run_command(
    command,
    cwd: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def build_skill_readme(meta: Dict[str, str], skill_path: Path) -> str:
    files = [
        str(path.relative_to(skill_path)).replace("\\", "/")
        for path in sorted(skill_path.rglob("*"))
        if path.is_file()
        and path.name != "README.md"
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


def copy_skill_to_repo_root(skill_path: Path, repo_dir: Path) -> None:
    for child in skill_path.iterdir():
        if should_skip(child.name) or child.name == "README.md":
            continue
        target = repo_dir / child.name
        if child.is_dir():
            shutil.copytree(
                child,
                target,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store"),
            )
        else:
            shutil.copy2(child, target)

    for pycache in repo_dir.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    for pyc in repo_dir.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def remote_url(owner: str, repo_name: str, ssh_host: str) -> str:
    return f"git@{ssh_host}:{owner}/{repo_name}.git"


def default_repo_dir(repo_name: str) -> Path:
    return Path.home() / ".codex" / "skill-git-repos" / repo_name


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    result = run_command(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=repo_dir,
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
) -> None:
    remote = remote_url(owner, repo_name, ssh_host)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    if not (repo_dir / ".git").exists():
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        run_command(["git", "clone", remote, str(repo_dir)])

    run_command(["git", "remote", "set-url", "origin", remote], cwd=repo_dir)
    run_command(["git", "fetch", "origin", "--prune"], cwd=repo_dir, check=False)

    if remote_branch_exists(repo_dir, branch):
        run_command(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
        run_command(["git", "pull", "--ff-only", "origin", branch], cwd=repo_dir, check=False)
    else:
        run_command(["git", "checkout", "-B", branch], cwd=repo_dir)

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
) -> bool:
    if not repo_has_changes(repo_dir):
        return False

    run_command(["git", "add", "."], cwd=repo_dir)
    run_command(["git", "commit", "-m", message], cwd=repo_dir)
    if push:
        run_command(["git", "push", "-u", "origin", branch], cwd=repo_dir)
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
    ensure_local_repo(repo_dir, args.owner, args.repo_name, args.branch, args.ssh_host)
    print(f"Repo ready: {args.owner}/{args.repo_name}")
    print(f"Token source: {source}")
    print(f"Local cache: {repo_dir}")
    print(f"Created remote repo: {'yes' if created else 'no'}")
    return 0


def run_publish_skill(args: argparse.Namespace) -> int:
    skill_path = resolve_skill_path(args.skill_name, args.skill_path)
    meta = parse_skill(skill_path)
    repo_name = meta["name"]

    token, source = resolve_token(args.token_env)
    created = ensure_remote_repo(args.owner, repo_name, args.visibility, token)
    repo_dir = resolve_repo_dir(repo_name, args.repo_dir)
    ensure_local_repo(repo_dir, args.owner, repo_name, args.branch, args.ssh_host)

    readme_text = (
        Path(args.readme_file).expanduser().resolve().read_text(encoding="utf-8")
        if args.readme_file
        else build_skill_readme(meta, skill_path)
    )

    remove_worktree_contents(repo_dir)
    copy_skill_to_repo_root(skill_path, repo_dir)
    (repo_dir / "README.md").write_text(readme_text, encoding="utf-8")

    commit_message = args.commit_message or f"Publish {repo_name}"
    changed = commit_and_push(repo_dir, args.branch, commit_message, push=not args.no_push)

    print(f"Skill: {repo_name}")
    print(f"Remote repo: {args.owner}/{repo_name}")
    print(f"Token source: {source}")
    print(f"Local cache: {repo_dir}")
    print(f"Created remote repo: {'yes' if created else 'no'}")
    print(f"Changed: {'yes' if changed else 'no'}")
    print(f"Pushed: {'no' if args.no_push else 'yes' if changed else 'no changes'}")
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
    publish_parser.set_defaults(func=run_publish_skill)

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
