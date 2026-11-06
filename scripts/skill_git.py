#!/usr/bin/env python3
import argparse, hashlib, json, os, re, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

from skill_assets import EXCLUDED_DIRS, EXCLUDED_EXTENSIONS, build_archive, is_local_only, layout_warnings, scan_skill, upload_archive, write_bootstrap
from skill_lint import DEFAULT_MAX_BYTES, cache_root, lint_skill, write_audit

DEFAULT_OWNER=os.environ.get("SKILL_GIT_OWNER","zjashanda"); DEFAULT_BRANCH=os.environ.get("SKILL_GIT_BRANCH","main")
DEFAULT_SSH_HOST=os.environ.get("SKILL_GIT_SSH_HOST","github-zjashanda"); DEFAULT_VISIBILITY=os.environ.get("SKILL_GIT_VISIBILITY","public")
DEFAULT_TOKEN_ENV=os.environ.get("SKILL_GIT_TOKEN_ENV","GITHUB_TOKEN"); DEFAULT_TRANSPORT=os.environ.get("SKILL_GIT_TRANSPORT","auto")
DEFAULT_OSS_ACCESS_KEY_ENV=os.environ.get("SKILL_GIT_OSS_ACCESS_KEY_ENV","ACCESS_KEY_ID"); DEFAULT_OSS_SECRET_ENV=os.environ.get("SKILL_GIT_OSS_SECRET_ENV","ACCESS_KEY_SECRET")
DEFAULT_OSS_ENDPOINT=os.environ.get("OSS_ENDPOINT","https://oss-cn-beijing.aliyuncs.com"); DEFAULT_OSS_REGION=os.environ.get("OSS_REGION","cn-beijing")
DEFAULT_OSS_BUCKET=os.environ.get("OSS_BUCKET","listenai-test-internal"); DEFAULT_OSS_PREFIX=os.environ.get("OSS_PREFIX","exe/bszheng/"); DEFAULT_OSS_ACL=os.environ.get("OSS_ACL","public-read")
DEFAULT_ASSET_MAX_BYTES=1024*1024; DEFAULT_ASSET_THRESHOLD=int(os.environ.get("SKILL_GIT_ASSET_THRESHOLD",str(DEFAULT_ASSET_MAX_BYTES))); DEFAULT_ASSET_MODE=os.environ.get("SKILL_GIT_ASSET_MODE","auto")
API_BASE="https://api.github.com"
TEXT_EXTENSIONS={".py",".ps1",".bat",".cmd",".sh",".js",".ts",".json",".jsonc",".yaml",".yml",".toml",".ini",".cfg",".conf",".env",".md",".txt",".xml",".csv",".sql",".html"}

class SkillGitError(Exception):
    exit_code=2
class UsageError(SkillGitError): exit_code=2
class ValidationError(SkillGitError): exit_code=3
class AuthError(SkillGitError): exit_code=4
class NetworkError(SkillGitError): exit_code=5
class GitError(SkillGitError): exit_code=6
class LockError(SkillGitError): exit_code=7

def run_command(command,cwd:Optional[Path]=None,check=True,env=None):
    merged=os.environ.copy(); merged.update(env or {})
    result=subprocess.run(command,cwd=str(cwd) if cwd else None,capture_output=True,text=True,encoding="utf-8",errors="replace",env=merged,check=False)
    if check and result.returncode: raise GitError((result.stderr or result.stdout or "command failed").strip())
    return result

def windows_env_value(name):
    if os.name!="nt": return None,None
    try: import winreg
    except ImportError: return None,None
    for hive,key,source in ((winreg.HKEY_CURRENT_USER,r"Environment","user-env-registry"),(winreg.HKEY_LOCAL_MACHINE,r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment","machine-env-registry")):
        try:
            with winreg.OpenKey(hive,key) as handle: value,_=winreg.QueryValueEx(handle,name)
            if value: return str(value),source
        except OSError: pass
    return None,None

def resolve_token(token_env):
    token=os.environ.get(token_env) or windows_env_value(token_env)[0]
    if token: return token,"process-env" if os.environ.get(token_env) else "windows-registry"
    raise AuthError(f"Token {token_env} was not found in the current process or Windows environment registry.")
def optional_token(token_env):
    try: return resolve_token(token_env)[0]
    except AuthError: return ""
def resolve_required_env(name):
    value=os.environ.get(name,"").strip()
    if not value: raise ValidationError(f"Environment variable {name} was not found in the current process.")
    return value
def sha256_file(path):
    digest=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()
def load_oss_sdk():
    try: import alibabacloud_oss_v2 as oss; return oss
    except ImportError as exc: raise ValidationError("OSS upload requires alibabacloud-oss-v2. Install it with: python -m pip install -r requirements.txt") from exc
def resolve_asset_path(root,value):
    candidate=(root/value).resolve()
    try: candidate.relative_to(root.resolve())
    except ValueError as exc: raise ValidationError(f"Asset path escapes skill root: {value}") from exc
    return candidate
def collect_asset_files(root,requested,include_large,max_bytes):
    paths=set()
    for value in requested:
        path=resolve_asset_path(root,value)
        if not path.exists(): raise FileNotFoundError(path)
        candidates=[path] if path.is_file() else list(path.rglob("*"))
        for child in candidates:
            if child.is_file() and not is_local_only(child.name) and not (set(child.relative_to(root).parts)&EXCLUDED_DIRS): paths.add(child)
    if include_large: paths.update(p for p in root.rglob("*") if p.is_file() and p.stat().st_size>max_bytes and not is_local_only(p.name) and not (set(p.relative_to(root).parts)&EXCLUDED_DIRS))
    if not paths: raise ValidationError("No asset files selected. Pass --asset or --include-large.")
    return sorted(paths)
def github_request(method,path,token,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None; req=urllib.request.Request(API_BASE+path,data=data,method=method); req.add_header("Accept","application/vnd.github+json"); req.add_header("Authorization",f"Bearer {token}"); req.add_header("X-GitHub-Api-Version","2022-11-28")
    if data is not None: req.add_header("Content-Type","application/json")
    try:
        with urllib.request.urlopen(req,timeout=30) as response: raw=response.read().decode(); return response.status,json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw=exc.read().decode(errors="replace")
        try: body=json.loads(raw) if raw else {}
        except json.JSONDecodeError: body={"message":raw[:500]}
        return exc.code,body
    except (urllib.error.URLError,TimeoutError) as exc: raise NetworkError(f"GitHub request failed: {exc}") from exc
def repo_exists(owner,repo,token):
    status,_=github_request("GET",f"/repos/{owner}/{repo}",token)
    if status==200:return True
    if status==404:return False
    if status in (401,403):raise AuthError(f"GitHub authentication or permission denied while checking {owner}/{repo}.")
    raise NetworkError(f"Unexpected GitHub status while checking repo: {status}")
def get_authenticated_user(token):
    status,body=github_request("GET","/user",token)
    if status in (401,403):raise AuthError("GitHub token is invalid or lacks permission to read /user.")
    if status!=200 or not isinstance(body,dict) or not body.get("login"):raise NetworkError(f"Unable to resolve authenticated GitHub user: HTTP {status}")
    return body
def ensure_remote_repo(owner,repo,visibility,token):
    if repo_exists(owner,repo,token):return False
    payload={"name":repo,"private":visibility=="private","description":f"Codex skill repository for {repo}.","auto_init":False}; user=get_authenticated_user(token); login=str(user["login"])
    if owner.casefold()==login.casefold(): path="/user/repos"
    else:
        status,_=github_request("GET",f"/orgs/{quote(owner)}",token)
        if status in (401,403,404):raise AuthError(f"Authenticated user cannot access organization '{owner}'.")
        if status!=200:raise NetworkError(f"Unable to verify organization '{owner}': HTTP {status}")
        path=f"/orgs/{quote(owner)}/repos"
    status,body=github_request("POST",path,token,payload)
    if status not in (201,202):
        message=body.get("message") if isinstance(body,dict) else body
        raise AuthError(f"Failed to create {owner}/{repo}: {message}") if status in (401,403) else NetworkError(f"Failed to create {owner}/{repo}: {message}")
    return True

def _fallback_yaml(text):
    result={}; lines=text.splitlines(); i=0
    while i<len(lines):
        line=lines[i]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:i+=1;continue
        key,raw=line.split(":",1); key=key.strip(); raw=raw.strip()
        if raw in ("|",">"):
            block=[]; i+=1
            while i<len(lines) and (not lines[i].strip() or lines[i].startswith((" ","\t"))):block.append(lines[i].lstrip());i+=1
            result[key]=("\n" if raw=="|" else " ").join(block).rstrip()+"\n";continue
        if len(raw)>=2 and raw[0]==raw[-1] and raw[0] in "\"'":raw=raw[1:-1]
        result[key]=raw;i+=1
    return result
def parse_skill(skill_path):
    file=skill_path/"SKILL.md"
    if not file.exists():raise FileNotFoundError(file)
    raw=file.read_text(encoding="utf-8-sig",errors="replace"); match=re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z",raw,re.S)
    if not match:raise ValidationError(f"Missing valid frontmatter in {file}")
    try:
        import yaml; meta=yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta,dict):raise ValueError("frontmatter must be a mapping")
    except ImportError:meta=_fallback_yaml(match.group(1))
    except Exception as exc:raise ValidationError(f"Invalid frontmatter in {file}: {exc}") from exc
    name=str(meta.get("name","")).strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*",name):raise ValidationError("Skill name must be non-empty and match [A-Za-z0-9][A-Za-z0-9._-]*.")
    body=match.group(2).lstrip(); title_match=re.search(r"^#\s+(.+)$",body,re.M); title=title_match.group(1).strip() if title_match else name
    return {"name":name,"description":str(meta.get("description","")).strip(),"body":body,"title":title}
def strip_first_heading(body):return re.sub(r"\A#\s+.+?\r?\n+","",body,count=1,flags=re.S).strip()
def build_skill_readme(meta,skill_path,excluded=None):
    excluded=excluded or set(); files=[]
    for p in sorted(skill_path.rglob("*")):
        rel=p.relative_to(skill_path).as_posix()
        if p.is_file() and p.name.casefold()!="readme.md" and not is_local_only(p.name) and not (set(p.relative_to(skill_path).parts)&EXCLUDED_DIRS) and rel not in excluded and p.suffix.lower() not in EXCLUDED_EXTENSIONS:files.append(rel)
    return "\n".join([f"# {meta['name']}","",meta["description"],"","## Skill layout","",*[f"- `{x}`" for x in files],"","## Install the skill","","Copy this folder into:","","```text",f"~/.codex/skills/{meta['name']}",f"~/.config/opencode/skills/{meta['name']}","```","","Then restart Codex or OpenCode.","","## Usage and workflow","",strip_first_heading(meta["body"]),""])
def should_skip(name):return name in {".git","__pycache__",".DS_Store"} or is_local_only(name) or Path(name).suffix.lower() in EXCLUDED_EXTENSIONS
def remove_worktree_contents(repo_dir):
    for child in repo_dir.iterdir():
        if child.name==".git":continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
def copy_skill_to_repo_root(skill_path,repo_dir,excluded=None):
    excluded=excluded or set()
    for source in skill_path.rglob("*"):
        rel=source.relative_to(skill_path).as_posix()
        if not source.is_file() or should_skip(source.name) or (set(source.relative_to(skill_path).parts)&EXCLUDED_DIRS) or rel in excluded or source.name.casefold()=="readme.md":continue
        target=repo_dir/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source,target)
def remote_url(owner,repo,host):return f"git@{host}:{owner}/{repo}.git"
def https_remote_url(owner,repo):return f"https://github.com/{owner}/{repo}.git"
def ssh_probe(owner,repo,host):return run_command(["git","ls-remote",remote_url(owner,repo,host),"HEAD"],env={"GIT_SSH_COMMAND":"ssh -o BatchMode=yes -o ConnectTimeout=5"},check=False).returncode==0
def select_transport(owner,repo,host,requested):
    if requested not in {"auto","ssh","https"}:raise UsageError(f"Unsupported Git transport: {requested}")
    return requested if requested!="auto" else ("ssh" if ssh_probe(owner,repo,host) else "https")
@contextmanager
def git_auth_env(transport,token):
    if transport!="https":yield {};return
    with tempfile.TemporaryDirectory(prefix="skill-git-askpass-") as d:
        ask=Path(d)/"askpass.cmd"
        ask.write_text('@echo off\r\n"%SKILL_GIT_PYTHON%" -c "import os,sys; prompt=sys.argv[1] if len(sys.argv)>1 else \'\'; print(\'x-access-token\' if \'Username\' in prompt else os.environ[\'SKILL_GIT_ASKPASS_TOKEN\'])" "%~1"\r\n',encoding="ascii")
        yield {"GIT_ASKPASS":str(ask),"GIT_TERMINAL_PROMPT":"0","SKILL_GIT_ASKPASS_TOKEN":token,"SKILL_GIT_PYTHON":sys.executable}
def default_repo_dir(repo):return cache_root()/repo
def ensure_git_identity(repo_dir,owner):
    name=run_command(["git","config","--get","user.name"],cwd=repo_dir,check=False).stdout.strip()
    email=run_command(["git","config","--get","user.email"],cwd=repo_dir,check=False).stdout.strip()
    run_command(["git","config","user.name",os.environ.get("SKILL_GIT_GIT_USER_NAME") or name or owner],cwd=repo_dir)
    run_command(["git","config","user.email",os.environ.get("SKILL_GIT_GIT_USER_EMAIL") or email or f"{owner}@users.noreply.github.com"],cwd=repo_dir)
def remote_branch_exists(repo_dir,branch,env=None):
    return bool(run_command(["git","ls-remote","--heads","origin",branch],cwd=repo_dir,env=env,check=False).stdout.strip())
def ensure_local_repo(repo_dir,owner,repo,branch,host,transport,token):
    remote=remote_url(owner,repo,host) if transport=="ssh" else https_remote_url(owner,repo); repo_dir.parent.mkdir(parents=True,exist_ok=True)
    with git_auth_env(transport,token) as auth:
        if not (repo_dir/".git").exists():
            if repo_dir.exists():shutil.rmtree(repo_dir)
            run_command(["git","clone",remote,str(repo_dir)],env=auth)
        run_command(["git","remote","set-url","origin",remote],cwd=repo_dir,env=auth)
        run_command(["git","fetch","origin","--prune"],cwd=repo_dir,env=auth,check=False)
        if remote_branch_exists(repo_dir,branch,auth):
            run_command(["git","checkout","-B",branch,f"origin/{branch}"],cwd=repo_dir,env=auth)
            run_command(["git","pull","--ff-only","origin",branch],cwd=repo_dir,env=auth,check=False)
        else:
            run_command(["git","checkout","-B",branch],cwd=repo_dir,env=auth)
    ensure_git_identity(repo_dir,owner)
def repo_has_changes(repo_dir):return bool(run_command(["git","status","--porcelain"],cwd=repo_dir).stdout.strip())
def resolve_skill_path(name,path):
    result=Path(path).expanduser().resolve() if path else (Path.home()/".codex"/"skills"/name).resolve() if name else None
    if result is None:raise UsageError("Provide --skill-name or --skill-path.")
    if not result.exists():raise FileNotFoundError(result)
    return result
def resolve_repo_dir(repo,repo_dir):return Path(repo_dir).expanduser().resolve() if repo_dir else default_repo_dir(repo)
def commit_and_push(repo_dir,branch,message,push,transport,token):
    if not repo_has_changes(repo_dir):return False
    run_command(["git","add","."],cwd=repo_dir);run_command(["git","commit","-m",message],cwd=repo_dir)
    if push:
        with git_auth_env(transport,token) as auth:run_command(["git","push","-u","origin",branch],cwd=repo_dir,env=auth)
    return True

def lock_dir():return Path(os.environ.get("SKILL_GIT_LOCK_DIR",str(cache_root()/".locks")))
@contextmanager
def repo_lock(repo):
    directory=lock_dir();directory.mkdir(parents=True,exist_ok=True);path=directory/f"{repo}.lock"; timeout=float(os.environ.get("SKILL_GIT_LOCK_TIMEOUT","1800"))
    for attempt in range(2):
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY); os.write(fd,json.dumps({"pid":os.getpid(),"hostname":socket.gethostname(),"timestamp":datetime.now(timezone.utc).isoformat()}).encode());os.close(fd);break
        except FileExistsError:
            stale=False
            try:
                data=json.loads(path.read_text()); stale=time.time()-path.stat().st_mtime>timeout or (data.get("hostname")==socket.gethostname() and not _pid_alive(int(data.get("pid",-1))))
            except (OSError,ValueError,TypeError):stale=True
            if stale and attempt==0:
                try:path.unlink()
                except OSError:pass
                continue
            raise LockError(f"Repository lock exists: {path}")
    try:yield path
    finally:
        try:path.unlink()
        except FileNotFoundError:pass
def _pid_alive(pid):
    if pid<=0:return False
    if os.name=="nt":
        try:return subprocess.run(["tasklist","/FI",f"PID eq {pid}"],capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=False).returncode==0 and str(pid) in subprocess.run(["tasklist","/FI",f"PID eq {pid}"],capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=False).stdout.decode(errors="ignore")
        except OSError:return False
    try:os.kill(pid,0);return True
    except OSError:return False

def _excluded(rel):
    p=Path(rel);return p.name.casefold()=="readme.md" or is_local_only(p.name) or bool(set(p.parts)&EXCLUDED_DIRS) or p.suffix.lower() in EXCLUDED_EXTENSIONS
def publishable_files(root):return {p.relative_to(root).as_posix():p for p in root.rglob("*") if p.is_file() and not _excluded(p.relative_to(root).as_posix())}
def same_file(a,b):
    if a.suffix.lower() in TEXT_EXTENSIONS and b.suffix.lower() in TEXT_EXTENSIONS:
        normalize=lambda value:value.replace(b"\r\n",b"\n").replace(b"\r",b"\n")
        return normalize(a.read_bytes())==normalize(b.read_bytes())
    if a.stat().st_size!=b.stat().st_size:return False
    return sha256_file(a)==sha256_file(b)
def calculate_diff(local,remote):
    left=publishable_files(local); right=publishable_files(remote); added=sorted(set(left)-set(right));deleted=sorted(set(right)-set(left));modified=sorted(k for k in set(left)&set(right) if not same_file(left[k],right[k]));return {"added":added,"modified":modified,"deleted":deleted,"unchanged":len(set(left)&set(right))-len(modified)}
def prepare_cache(args,repo_name,token=""):
    repo=resolve_repo_dir(repo_name,args.repo_dir)
    if repo.exists() and (repo/".git").exists():return repo
    if getattr(args,"dry_run",False):return repo
    ensure_local_repo(repo,args.owner,repo_name,args.branch,args.ssh_host,args.transport,token);return repo
def print_diff(diff,as_json=False):
    if as_json:print(json.dumps(diff,ensure_ascii=False));return
    for key in ("added","modified","deleted"):print(f"{key}: {len(diff[key])}");[print(f"  {x}") for x in diff[key]]
    print(f"unchanged: {diff['unchanged']}")
def run_diff(args):
    local=resolve_skill_path(args.skill_name,args.skill_path); name=parse_skill(local)["name"]
    token=optional_token(args.token_env); repo=prepare_cache(args,name,token); diff=calculate_diff(local,repo) if repo.exists() else {"added":sorted(publishable_files(local)),"modified":[],"deleted":[],"unchanged":0};print_diff(diff,args.json);return 8 if args.fail_on_diff and any(diff[k] for k in ("added","modified","deleted")) else 0
def run_pull(args):
    if not args.yes and not args.dry_run:raise UsageError("pull-skill requires --yes unless --dry-run is used.")
    local=resolve_skill_path(args.skill_name,args.skill_path);name=parse_skill(local)["name"];token=optional_token(args.token_env)
    if args.dry_run:
        repo=prepare_cache(args,name,token);diff=calculate_diff(local,repo) if repo.exists() else {"added":[],"modified":[],"deleted":[],"unchanged":0};print_diff(diff,args.json);return 0
    with repo_lock(name):
        repo=prepare_cache(args,name,token);diff=calculate_diff(local,repo) if repo.exists() else {"added":[],"modified":[],"deleted":[],"unchanged":0}
        backup=Path(args.backup_dir).expanduser().resolve() if args.backup_dir else Path.home()/".codex"/"skill-backups"/f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}";shutil.copytree(local,backup)
        for rel,src in publishable_files(repo).items():
            dst=local/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
        if args.include_readme and (repo/"README.md").exists():shutil.copy2(repo/"README.md",local/"README.md")
        if args.mirror:
            for rel in set(publishable_files(local))-set(publishable_files(repo)):
                (local/rel).unlink()
        result={"backup":str(backup),**{k:len(diff[k]) if isinstance(diff[k],list) else diff[k] for k in diff}};print(json.dumps(result) if args.json else f"Backup: {backup}\nAdded: {result['added']} Modified: {result['modified']} Deleted: {result['deleted']}");return 0

def run_show_config(args):
    env_token=os.environ.get(args.token_env);registry_token,registry_source=windows_env_value(args.token_env);present=bool(env_token or registry_token);source="process-env" if env_token else registry_source;data={"owner":args.owner,"branch":args.branch,"ssh_host":args.ssh_host,"visibility":args.visibility,"token_env":args.token_env,"token_present":present,"token_source":source,"repo_rule":"repo name = skill name","layout_rule":"repo root = skill contents"};print(json.dumps(data,ensure_ascii=False) if args.json else json.dumps(data,ensure_ascii=False,indent=2));return 0
def run_ensure_repo(args):
    token,_=resolve_token(args.token_env);repo=args.repo_name
    with repo_lock(repo):
        created=ensure_remote_repo(args.owner,repo,args.visibility,token);transport=select_transport(args.owner,repo,args.ssh_host,args.transport);ensure_local_repo(resolve_repo_dir(repo,args.repo_dir),args.owner,repo,args.branch,args.ssh_host,transport,token)
    data={"repo":f"{args.owner}/{repo}","created":created};print(json.dumps(data) if args.json else f"Repo ready: {data['repo']}\nCreated remote repo: {'yes' if created else 'no'}");return 0
def run_upload_assets(args):
    if args.oss_acl=="public-read-write" and not args.allow_public_write:raise ValidationError("--oss-acl public-read-write requires --allow-public-write")
    root=resolve_skill_path(args.skill_name,args.skill_path);repo=parse_skill(root)["name"];assets=collect_asset_files(root,args.asset,args.include_large,args.max_bytes);stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ");name=args.archive_name or f"{repo}-assets-{stamp}.zip";key="/".join(x.strip("/") for x in (args.oss_prefix,name) if x.strip("/"));result={"files":[p.relative_to(root).as_posix() for p in assets],"total_size":sum(p.stat().st_size for p in assets),"archive_name":name,"manifest_key":f"{key}.json"}
    if args.dry_run:print(json.dumps(result,ensure_ascii=False) if args.json else f"Files: {len(assets)}\nTotal size: {result['total_size']}\nArchive: {name}\nManifest key: {result['manifest_key']}");return 0
    with tempfile.TemporaryDirectory() as d:
        archive=Path(d)/name;manifest=build_archive(root,assets,archive);result=upload_archive(archive,name,args.oss_prefix,args.oss_bucket,args.oss_endpoint,args.oss_region,args.oss_acl,manifest,args.oss_access_key_env,args.oss_secret_key_env);result["skill"]=repo
    print(json.dumps(result,ensure_ascii=False) if args.json else f"OSS archive: {result['archive_url']}\nOSS manifest: {result['manifest_url']}");return 0
def lint_kwargs(args,readme_level="error"):
    return {"max_bytes":args.max_size,"allow_large":args.allow_large,"registry_path":Path(args.registry).expanduser() if args.registry else None,"known_skills":args.known_skill,"readme_level":readme_level,"asset_threshold":getattr(args,"asset_threshold",None) or 1024*1024}
def run_lint(args):
    root=resolve_skill_path(args.skill_name,args.skill_path);report=lint_skill(root,**lint_kwargs(args))
    if args.audit and not args.dry_run:
        try:write_audit(report,args.audit_dir,command="lint-skill",fail_on_error=True);report.stats["audit"]="written"
        except OSError as exc:report.add("audit","warning",f"audit record was not written: {exc}");report.stats["audit"]="failed"
    else:report.stats["audit"]="skipped (--dry-run)" if args.dry_run else "not requested"
    if args.json:print(json.dumps(report.to_dict(args.fail_on_error),ensure_ascii=False))
    else:[print(line) for line in report.lines()]
    return report.exit_code(args.fail_on_error)
def enforce_lint(args,root,name):
    report=lint_skill(root,**lint_kwargs(args,readme_level="warning"))
    try:write_audit(report,args.audit_dir,command="publish-skill",fail_on_error=True,extra={"dry_run":bool(args.dry_run)})
    except OSError as exc:print(f"WARNING: audit record was not written: {exc}",file=sys.stderr)
    if report.errors:
        detail="; ".join(f"[{v.rule}] {v.message}" for v in report.errors[:6])
        raise ValidationError(f"lint-skill rejected {name}: {detail}")
    for item in report.warnings:print(f"WARNING: [{item.rule}] {item.message}",file=sys.stderr if args.json else sys.stdout)
    return report
def run_publish_skill(args):
    root=resolve_skill_path(args.skill_name,args.skill_path);meta=parse_skill(root);report=enforce_lint(args,root,meta["name"]);scan=scan_skill(root,args.asset_threshold)
    lint_summary={"verdict":report.verdict,"errors":len(report.errors),"warnings":len(report.warnings),"audit_path":report.audit_path}
    if scan.secrets:raise ValidationError(f"refusing to publish secrets in: {', '.join(scan.secrets)}")
    if scan.foreground_commands:raise ValidationError(f"foreground terminal command patterns found in: {', '.join(scan.foreground_commands)}")
    warnings=layout_warnings(root);[print(f"WARNING: {w}",file=sys.stderr if args.json else sys.stdout) for w in warnings];excluded={p.relative_to(root).as_posix() for p in scan.assets};manifest=None
    if scan.assets and args.asset_mode=="off":raise ValidationError(f"{len(scan.assets)} file(s) exceed {args.asset_threshold} bytes; use --asset-mode auto or move them to OSS first")
    if args.dry_run:
        repo=resolve_repo_dir(meta["name"],args.repo_dir);diff=calculate_diff(root,repo) if repo.exists() else {"added":sorted(publishable_files(root)),"modified":[],"deleted":[],"unchanged":0};data={"diff":diff,"asset_count":len(scan.assets),"archive_name":f"{meta['name']}-assets.zip","lint":lint_summary};print(json.dumps(data) if args.json else f"Lint: {lint_summary['verdict']} ({lint_summary['warnings']} warnings, audit {lint_summary['audit_path'] or 'none'})\nPlanned added: {len(diff['added'])}\nPlanned modified: {len(diff['modified'])}\nPlanned deleted: {len(diff['deleted'])}\nAsset count: {len(scan.assets)}\nArchive: {data['archive_name']}");return 0
    token,source=resolve_token(args.token_env)
    with repo_lock(meta["name"]):
        if scan.assets:
            with tempfile.TemporaryDirectory() as d:
                archive=Path(d)/(meta["name"]+"-assets.zip");manifest=upload_archive(archive,f"{meta['name']}-assets-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip",args.oss_prefix,args.oss_bucket,args.oss_endpoint,args.oss_region,args.oss_acl,build_archive(root,scan.assets,archive),args.oss_access_key_env,args.oss_secret_key_env)
            manifest["skill"]=meta["name"]
        created=ensure_remote_repo(args.owner,meta["name"],args.visibility,token);transport=select_transport(args.owner,meta["name"],args.ssh_host,args.transport);repo=resolve_repo_dir(meta["name"],args.repo_dir);ensure_local_repo(repo,args.owner,meta["name"],args.branch,args.ssh_host,transport,token);remove_worktree_contents(repo);copy_skill_to_repo_root(root,repo,excluded)
        if manifest:write_bootstrap(repo,manifest)
        (repo/"README.md").write_text(Path(args.readme_file).read_text(encoding="utf-8") if args.readme_file else build_skill_readme(meta,root,excluded),encoding="utf-8");changed=commit_and_push(repo,args.branch,args.commit_message or f"Publish {meta['name']}",not args.no_push,transport,token)
    data={"skill":meta["name"],"created":created,"changed":changed,"pushed":not args.no_push and changed,"token_source":source,"lint":lint_summary};print(json.dumps(data) if args.json else f"Skill: {data['skill']}\nChanged: {'yes' if changed else 'no'}\nPushed: {'yes' if data['pushed'] else 'no'}");return 0

def common(parser):
    parser.add_argument("--owner",default=DEFAULT_OWNER);parser.add_argument("--branch",default=DEFAULT_BRANCH);parser.add_argument("--ssh-host",default=DEFAULT_SSH_HOST);parser.add_argument("--transport",choices=("auto","ssh","https"),default=DEFAULT_TRANSPORT);parser.add_argument("--visibility",choices=("public","private"),default=DEFAULT_VISIBILITY);parser.add_argument("--token-env",default=DEFAULT_TOKEN_ENV);parser.add_argument("--repo-dir");parser.add_argument("--json",action="store_true")
def lint_options(parser,gate=False):
    parser.add_argument("--max-size",type=int,default=DEFAULT_MAX_BYTES);parser.add_argument("--allow-large",action="store_true");parser.add_argument("--registry");parser.add_argument("--known-skill",action="append",default=[]);parser.add_argument("--audit-dir")
    if gate:parser.add_argument("--audit",action="store_true");parser.add_argument("--dry-run",action="store_true");parser.add_argument("--fail-on-error",action="store_true")
def build_parser():
    p=argparse.ArgumentParser(description="Publish Codex skills to same-name GitHub repositories.");s=p.add_subparsers(dest="command",required=True)
    q=s.add_parser("show-config");common(q);q.set_defaults(func=run_show_config)
    q=s.add_parser("ensure-repo");common(q);q.add_argument("--repo-name",required=True);q.set_defaults(func=run_ensure_repo)
    q=s.add_parser("lint-skill");common(q);q.add_argument("--skill-name");q.add_argument("--skill-path");lint_options(q,gate=True);q.set_defaults(func=run_lint)
    q=s.add_parser("publish-skill");common(q);q.add_argument("--skill-name");q.add_argument("--skill-path");q.add_argument("--readme-file");q.add_argument("--commit-message");q.add_argument("--no-push",action="store_true");q.add_argument("--dry-run",action="store_true");q.add_argument("--asset-mode",choices=("auto","off"),default=DEFAULT_ASSET_MODE);q.add_argument("--asset-threshold",type=int,default=DEFAULT_ASSET_THRESHOLD);lint_options(q);q.add_argument("--oss-access-key-env",default=DEFAULT_OSS_ACCESS_KEY_ENV);q.add_argument("--oss-secret-key-env",default=DEFAULT_OSS_SECRET_ENV);q.add_argument("--oss-endpoint",default=DEFAULT_OSS_ENDPOINT);q.add_argument("--oss-region",default=DEFAULT_OSS_REGION);q.add_argument("--oss-bucket",default=DEFAULT_OSS_BUCKET);q.add_argument("--oss-prefix",default=DEFAULT_OSS_PREFIX);q.add_argument("--oss-acl",choices=("private","public-read","public-read-write"),default=DEFAULT_OSS_ACL);q.set_defaults(func=run_publish_skill)
    q=s.add_parser("upload-assets");common(q);q.add_argument("--skill-name");q.add_argument("--skill-path");q.add_argument("--asset",action="append",default=[]);q.add_argument("--include-large",action="store_true");q.add_argument("--max-bytes",type=int,default=DEFAULT_ASSET_MAX_BYTES);q.add_argument("--archive-name",default="");q.add_argument("--dry-run",action="store_true");q.add_argument("--allow-public-write",action="store_true");q.add_argument("--oss-access-key-env",default=DEFAULT_OSS_ACCESS_KEY_ENV);q.add_argument("--oss-secret-key-env",default=DEFAULT_OSS_SECRET_ENV);q.add_argument("--oss-endpoint",default=DEFAULT_OSS_ENDPOINT);q.add_argument("--oss-region",default=DEFAULT_OSS_REGION);q.add_argument("--oss-bucket",default=DEFAULT_OSS_BUCKET);q.add_argument("--oss-prefix",default=DEFAULT_OSS_PREFIX);q.add_argument("--oss-acl",choices=("private","public-read","public-read-write"),default=DEFAULT_OSS_ACL);q.set_defaults(func=run_upload_assets)
    for name,func in (("diff-skill",run_diff),("pull-skill",run_pull)):
        q=s.add_parser(name);common(q);q.add_argument("--skill-name");q.add_argument("--skill-path");
        if name=="diff-skill":q.add_argument("--fail-on-diff",action="store_true")
        else:q.add_argument("--backup-dir");q.add_argument("--yes",action="store_true");q.add_argument("--mirror",action="store_true");q.add_argument("--include-readme",action="store_true");q.add_argument("--dry-run",action="store_true")
        q.set_defaults(func=func)
    return p
def main():
    args=build_parser().parse_args()
    return args.func(args)
if __name__=="__main__":
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(130)
    except SkillGitError as exc:print(f"ERROR: {exc}",file=sys.stderr);raise SystemExit(exc.exit_code)
    except Exception as exc:print(f"ERROR: {exc}",file=sys.stderr);raise SystemExit(1)
