# skill-git

Publish local Codex skills to GitHub with one token and one SSH transport, using the skill name as the repository name. Use when Codex needs to create or verify a GitHub repo for a skill, export a local skill into a same-name repository, generate a GitHub README from SKILL.md, commit and push updates, or explain the standard skill publishing workflow.

## Skill layout

- `SKILL.md`
- `agents/openai.yaml`
- `scripts/skill_git.py`

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

## Workflow

1. Run `show-config` to confirm the token source, SSH alias, and local repo cache path.
2. Run `publish-skill` with `--skill-name` or `--skill-path`.
3. Let the script read `SKILL.md`, derive the skill name, and use that name as the GitHub repository name.
4. When the local cache repo may already contain local changes, use the safe sync order first: stash local changes, sync remote latest into the cache repo, then restore the stash and resolve any merge conflicts before the final commit/push.
5. Let the script create the GitHub repo if it does not exist, clone or refresh the local cache, replace the repo root with the skill contents, generate `README.md`, commit, and push.
6. Use `--readme-file` only when a custom GitHub README is required instead of the generated one.

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

## Rules

- Publish each skill into its own same-name repository. Do not add an extra skill root directory inside the repository.
- Use the GitHub token only for GitHub API calls such as checking or creating repositories.
- Use SSH for clone, fetch, pull, and push so the token is not embedded in git remotes.
- To avoid accidental overwrite during sync, prefer this order whenever the local cache repo has pending work: stash local changes -> sync remote latest -> restore stash and merge -> commit -> push.
- Generate a root `README.md` for GitHub by default so the repository is readable outside Codex.
- Exclude `.git`, `__pycache__`, `.pyc`, and similar cache artifacts from published repositories.
- Do not print or persist the token value.

## Resource

- `scripts/skill_git.py`: Create or verify a same-name GitHub repo for a skill, sync the skill contents to the repo root, generate `README.md`, commit, and push.
