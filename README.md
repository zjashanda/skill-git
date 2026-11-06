# skill-git

Publish local Codex skills to GitHub with one token and one SSH transport, using the skill name as the repository name. Use when Codex needs to create or verify a GitHub repo for a skill, export a local skill into a same-name repository, generate a GitHub README from SKILL.md, commit and push updates, or explain the standard skill publishing workflow.

## Skill layout

- `agents/openai.yaml`
- `references/asset-policy.md`
- `requirements.txt`
- `scripts/skill_assets.py`
- `scripts/skill_git.py`
- `SKILL.md`

## Install the skill

Copy this folder into:

```text
~/.codex/skills/skill-git
```

Then restart Codex.

## Usage and workflow

Use `scripts/skill_git.py` whenever a local skill needs to be published to GitHub.

## Defaults

- GitHub owner: `zjashanda`
- SSH host alias: `github-zjashanda`
- token env: `GITHUB_TOKEN`
- repo naming rule: repo name = skill name
- repo layout rule: repo root directly contains `SKILL.md`, `README.md`, `agents/`, `scripts/`, and other skill files
- local repo cache: `~/.codex/skill-git-repos/<repo-name>`

## Node Environment

Required runtime:

```text
Python 3.10+
Git
```

Install the Python dependencies, including the OSS SDK:

```powershell
python -m pip install -r requirements.txt
```

Required environment variables:

| Variable | Used for | Required when |
|---|---|---|
| `GITHUB_TOKEN` | GitHub REST API | `ensure-repo` / `publish-skill` |
| `ACCESS_KEY_ID` | Alibaba Cloud OSS | `upload-assets` |
| `ACCESS_KEY_SECRET` | Alibaba Cloud OSS | `upload-assets` |

OSS defaults can be overridden by environment variables:

```text
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_REGION=cn-beijing
OSS_BUCKET=listenai-test-internal
OSS_PREFIX=exe/bszheng/
OSS_ACL=public-read
```

Do not put any token or AccessKey in `SKILL.md`, config files, Git remotes,
README files, command examples, or logs. Use node-level environment variables.
`public-read` allows other nodes to download assets; do not use
`public-read-write` unless an explicit security decision permits arbitrary uploads.

## Git Transport

`SKILL_GIT_TRANSPORT` accepts `auto`, `ssh`, or `https` and defaults to `auto`.

- `auto`: use SSH when an SSH key can authenticate to the repository; otherwise use HTTPS with `GITHUB_TOKEN` through a temporary askpass process.
- `ssh`: use the configured SSH host alias and local private key.
- `https`: use HTTPS and the token without embedding it in the remote URL.

The other Git defaults are configurable with `SKILL_GIT_OWNER`,
`SKILL_GIT_BRANCH`, `SKILL_GIT_SSH_HOST`, `SKILL_GIT_VISIBILITY`, and
`SKILL_GIT_TOKEN_ENV`. Each node may therefore use its own SSH key or token.

## Asset Policy

`publish-skill` scans the skill before publishing. The default Git threshold is
1 MiB so the result remains importable by clients with a 1 MiB per-file limit.

- Keep source, config, documentation and small assets in Git.
- Put files at or above 1 MiB into OSS automatically.
- `plan.md` (case-insensitive, at any depth) and `.omo/` are local planning files and are never published for any skill.
- Never commit logs, `result/`, `multi_logs/`, caches, generated build
  intermediates, secrets, or large binaries.
- GitHub warns at 50 MiB and blocks ordinary Git objects at 100 MiB; those are
  hard external limits, not permission to publish a large skill.
- The OSS archive is public-read; the repository stores a small
  `assets/asset-manifest.json` plus a restore script.
- `--asset-mode off` blocks publishing when large assets are present.
- `--asset-threshold` and the `OSS_*` options are configurable.

See [references/asset-policy.md](references/asset-policy.md) for the complete
policy and source references.

## Publish Validation

`publish-skill` blocks secrets and known foreground-terminal patterns. It also
prints layout warnings for non-standard root files/directories, missing Python
dependency manifests, and missing environment documentation. These warnings are
intentional: keep the repository root small and put implementation under
`scripts/`, `references/`, `assets/`, or another recognized directory.

## Workflow

1. Run `show-config` to confirm the token source, SSH alias, and local repo cache path.
2. Run `publish-skill` with `--skill-name` or `--skill-path`.
3. Let the script read `SKILL.md`, derive the skill name, and use that name as the GitHub repository name.
4. Let the script create the GitHub repo if it does not exist, clone or refresh the local cache, replace the repo root with the skill contents, generate `README.md`, commit, and push.
5. Use `--readme-file` only when a custom GitHub README is required instead of the generated one.

## Commands

- `python scripts/skill_git.py show-config`
  - Show the resolved owner, SSH alias, token source, and local repo cache rule without printing the token value.
- `python scripts/skill_git.py ensure-repo --repo-name listenai-play`
  - Ensure the GitHub repo exists and the local cache repo is ready.
- `python scripts/skill_git.py publish-skill --skill-name listenai-play`
  - Resolve `~/.codex/skills/listenai-play`, export it to the `listenai-play` repository root, generate `README.md`, commit, and push.
- `python scripts/skill_git.py publish-skill --skill-path C:\Users\Administrator\.codex\skills\listenai-play`
  - Publish a skill by absolute path when the installed location is known directly.
- `python scripts/skill_git.py publish-skill --skill-name listenai-play --readme-file D:\docs\README.md`
  - Publish with a custom GitHub README.
- `python scripts/skill_git.py publish-skill --skill-name listenai-play --no-push`
  - Export and commit locally without pushing.
- `python scripts/skill_git.py upload-assets --skill-path C:\path\to\skill --include-large`
  - Package selected large assets and upload a public-read archive plus JSON manifest to OSS.

`upload-assets` reads `ACCESS_KEY_ID` and `ACCESS_KEY_SECRET`, excludes Git,
cache, result, multi_logs, and logs directories, verifies the archive locally,
and prints the public archive/manifest URLs. It does not commit credentials or
large files to GitHub.

## Rules

- Publish each skill into its own same-name repository. Do not add an extra skill root directory inside the repository.
- Use the GitHub token only for GitHub API calls such as checking or creating repositories.
- Use SSH for clone, fetch, pull, and push so the token is not embedded in git remotes.
- Generate a root `README.md` for GitHub by default so the repository is readable outside Codex.
- Exclude `.git`, `__pycache__`, `.pyc`, and similar cache artifacts from published repositories.
- Do not print or persist the token value.

## Resource

- `scripts/skill_git.py`: Create or verify a same-name GitHub repo for a skill, sync the skill contents to the repo root, generate `README.md`, commit, and push.
