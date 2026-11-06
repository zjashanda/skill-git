# skill-git

管理本地 Codex skill 的 GitHub 发布、差异检查、回拉和 OSS 资产归档。需要创建或校验同名仓库、预览发布、比较缓存、恢复本地 skill、扫描凭据或执行安全上传时使用。

## Skill layout

- `agents/openai.yaml`
- `references/asset-policy.md`
- `requirements.txt`
- `scripts/skill_assets.py`
- `scripts/skill_git.py`
- `SKILL.md`
- `tests/__init__.py`
- `tests/test_skill_git.py`

## Install the skill

Copy this folder into:

```text
~/.codex/skills/skill-git
~/.config/opencode/skills/skill-git
```

Then restart Codex or OpenCode.

## Usage and workflow

使用 `scripts/skill_git.py` 管理本地 skill。默认 owner 为 `zjashanda`、分支为 `main`、SSH 别名为 `github-zjashanda`，仓库名取 SKILL.md 的合法 `name`，仓库根直接放 skill 内容。

## 命令

- `show-config [--json]`：显示解析后的配置和 token 是否存在，不打印 token。
- `ensure-repo --repo-name NAME [--json]`：创建/校验 GitHub 仓库并准备缓存。
- `publish-skill --skill-name NAME|--skill-path PATH [--dry-run] [--json]`：扫描、打包、生成 README、提交并推送；dry-run 不创建远端、不上传、不提交、不清空缓存。
- `diff-skill --skill-name NAME|--skill-path PATH [--fail-on-diff] [--json]`：比较本地 skill 与缓存；有差异且指定 `--fail-on-diff` 时退出 8。
- `pull-skill --skill-name NAME|--skill-path PATH --yes [--mirror] [--include-readme] [--dry-run] [--json]`：备份后回拉；dry-run 不写入，写操作必须 `--yes`。
- `upload-assets --skill-path PATH [--dry-run] [--allow-public-write] [--json]`：预览或上传资产；`public-read-write` 必须显式允许。

`--json` 模式 stdout 只输出一个 JSON 对象。默认缓存为 `~/.codex/skill-git-repos/<repo>`，锁为该目录下 `.locks/<repo>.lock`；可用 `SKILL_GIT_LOCK_DIR` 和 `SKILL_GIT_LOCK_TIMEOUT` 覆盖锁设置。

## 环境与安全

需要 Python 3.10+、Git；依赖见 `requirements.txt`。GitHub 使用 `GITHUB_TOKEN`，OSS 使用 `ACCESS_KEY_ID` 和 `ACCESS_KEY_SECRET`。OSS 默认环境变量为 `OSS_ENDPOINT`、`OSS_REGION`、`OSS_BUCKET`、`OSS_PREFIX`、`OSS_ACL`，也可用对应 `--oss-*` 参数覆盖。

不得把 token、AccessKey 写入文档、README、远端 URL 或日志。`public-read-write` 默认拒绝，只有 `--allow-public-write` 才允许。

扫描范围包括 `.py .ps1 .bat .cmd .sh .js .ts .json .jsonc .yaml .yml .toml .ini .cfg .conf .env .md .txt .xml .csv .sql .html`；超过 2 MiB 或包含 NUL 的文件不作为文本扫描。扫描会识别常见 GitHub、Alibaba、OpenAI、Slack 凭据和前台终端命令。`.git`、`.omo`、`.opencode`、`.venv`、`venv`、`runtime`、`output`、`secrets`、`dist`、`build`、缓存、日志目录、`plan.md`、`.env`、`*.local.json`、`*.secret.json` 和 `.log/.pyc/.pyo/.tmp/.key/.pem` 不发布。

## 资产与 README

达到资产阈值的文件会使用 `skill_assets.build_archive` 和 `upload_archive` 归档，并生成 `assets/asset-manifest.json` 与恢复脚本。默认 Git 阈值为 1 MiB；`--asset-mode off` 会在存在大文件时拒绝发布。README 在发布时重新生成；`--readme-file` 可提供自定义 README。

## 退出码

0 成功；2 用法；3 校验；4 认证；5 网络；6 Git；7 锁；8 `diff-skill --fail-on-diff` 检测到差异；130 用户中断；1 未预期异常。

## 验证

```powershell
python -m py_compile scripts\skill_git.py scripts\skill_assets.py
python -m unittest discover -s tests -v
```

资源：`scripts/skill_git.py`、`scripts/skill_assets.py`、`references/asset-policy.md`、`requirements.txt`。
