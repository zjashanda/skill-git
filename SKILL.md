---
name: skill-git
description: 管理本地 Codex skill 的 GitHub 发布、差异检查、回拉和 OSS 资产归档。需要创建或校验同名仓库、发布前跑 8 条门禁校验、预览发布、比较缓存、恢复本地 skill、扫描凭据或执行安全上传时使用。
---

# Skill Git

使用 `scripts/skill_git.py` 管理本地 skill。默认 owner 为 `zjashanda`、分支为 `main`、SSH 别名为 `github-zjashanda`，仓库名取 SKILL.md 的合法 `name`，仓库根直接放 skill 内容。

## 命令

- `show-config [--json]`：显示解析后的配置和 token 是否存在，不打印 token。
- `ensure-repo --repo-name NAME [--json]`：创建/校验 GitHub 仓库并准备缓存。
- `lint-skill --skill-name NAME|--skill-path PATH [--fail-on-error] [--audit] [--dry-run] [--json]`：只读校验 8 条发布规则；默认只报告（退出 0），加 `--fail-on-error` 后按门禁返回 3/8。
- `publish-skill --skill-name NAME|--skill-path PATH [--dry-run] [--json]`：先跑同一套 8 条校验，未通过直接退出，再扫描、打包、生成 README、提交并推送；dry-run 不创建远端、不上传、不提交、不清空缓存。
- `diff-skill --skill-name NAME|--skill-path PATH [--fail-on-diff] [--json]`：比较本地 skill 与缓存；有差异且指定 `--fail-on-diff` 时退出 8。
- `pull-skill --skill-name NAME|--skill-path PATH --yes [--mirror] [--include-readme] [--dry-run] [--json]`：备份后回拉；dry-run 不写入，写操作必须 `--yes`。
- `upload-assets --skill-path PATH [--dry-run] [--allow-public-write] [--json]`：预览或上传资产；`public-read-write` 必须显式允许。

`--json` 模式 stdout 只输出一个 JSON 对象。默认缓存为 `~/.codex/skill-git-repos/<repo>`（可用 `SKILL_GIT_CACHE_DIR` 改根目录），锁为该目录下 `.locks/<repo>.lock`；可用 `SKILL_GIT_LOCK_DIR` 和 `SKILL_GIT_LOCK_TIMEOUT` 覆盖锁设置。

## 发布前校验（8 条规则）

`lint-skill` 与 `publish-skill` 共用 `scripts/skill_lint.py`，规则不靠人记。

| # | 规则 | 判定 | 失败等级 |
|---|---|---|---|
| 1 | frontmatter | `SKILL.md` 存在且 frontmatter 可解析；`name` 匹配 `^[A-Za-z0-9][A-Za-z0-9._-]*$` 且等于仓库名（仓库名默认取目录名，`--skill-name` 场景取台账名） | error |
| 2 | 排除项 | 出现 `.venv/ venv/ .git/ .omo/ .opencode/ build/ dist/ __pycache__/ *.egg-info/ plan.md .env *.log *.key *.pem secrets/` 即拒；仓库自身的根 `.git` 不算内容；git 工作树里只判定"会被提交"的内容（已跟踪 + 未忽略的未跟踪），本地跑测试生成的 `__pycache__` 不会误伤 | error |
| 3 | 体积门禁 | 全仓超过 `--max-size`（默认 50 MiB）即拒；被排除目录的体积照样计入，所以"提交了 .venv"会同时触发规则 2 与规则 3；显式 `--allow-large` 才降级为告警 | error |
| 4 | 禁内嵌副本 | 任意层级出现第二个 `SKILL.md`，或子目录名命中已知 skill（台账 + 缓存仓库名 + `--known-skill`）且带 `scripts/`、`references/`、`requirements.txt`、`pyproject.toml` 之一 | error |
| 5 | 依赖声明 | `scripts/` 下有 `.py` 时，根目录必须有 `requirements.txt` 或 `pyproject.toml` | error |
| 6 | README 一致性 | 用同一份生成器反向比对 README：标题、description、Skill layout 文件集合、usage 正文；手写追加章节允许保留，纯手写 README（没有生成段落）跳过并告警 | error（`publish-skill` 内为 warning，因为发布本身会重写 README） |
| 7 | 台账登记 | `registry/skills.json` 中存在该 skill，`status` 不是 `archived`，`reuse` 与 `scope_note` 非空 | error |
| 8 | 审计留档 | `--audit` 把"谁、何时、哪些文件、什么结论"写入审计记录；`--dry-run` 只报告不落盘 | 记录 |

退出码：报告型调用（无 `--fail-on-error`）默认 0；门禁模式下有 error 返回 3，只有 README 漂移返回 8。`publish-skill` 永远按门禁处理，且校验发生在解析 token、创建远端、克隆缓存之前。

台账路径按 `--registry` → `SKILL_GIT_REGISTRY` → `~/.codex/skill-git-repos/ark-kb/registry/skills.json` 解析。文件不存在时规则 7 降级为告警并在输出与审计记录里写明"rule 7 skipped"，不会静默通过；文件存在但不可解析或缺失字段则按失败处理。

审计目录默认 `~/.codex/skill-git-audit`（可用 `SKILL_GIT_AUDIT_DIR` 或 `--audit-dir` 覆盖），记录名形如 `20261106T091348Z-<skill>.json`，内容含 `command`、`timestamp`、`actor`、`skill`、`stats`、`violations`、`verdict`、`exit_code`。`SKILL_GIT_ACTOR` 可覆盖 actor，默认 `user@hostname`。

## 环境与安全

需要 Python 3.10+、Git；依赖见 `requirements.txt`。GitHub 使用 `GITHUB_TOKEN`，OSS 使用 `ACCESS_KEY_ID` 和 `ACCESS_KEY_SECRET`。OSS 默认环境变量为 `OSS_ENDPOINT`、`OSS_REGION`、`OSS_BUCKET`、`OSS_PREFIX`、`OSS_ACL`，也可用对应 `--oss-*` 参数覆盖。

不得把 token、AccessKey 写入文档、README、远端 URL 或日志。`public-read-write` 默认拒绝，只有 `--allow-public-write` 才允许。

扫描范围包括 `.py .ps1 .bat .cmd .sh .js .ts .json .jsonc .yaml .yml .toml .ini .cfg .conf .env .md .txt .xml .csv .sql .html`；超过 2 MiB 或包含 NUL 的文件不作为文本扫描。扫描会识别常见 GitHub、Alibaba、OpenAI、Slack 凭据和前台终端命令。`.git`、`.omo`、`.opencode`、`.venv`、`venv`、`runtime`、`output`、`secrets`、`dist`、`build`、缓存、日志目录、`plan.md`、`.env`、`*.local.json`、`*.secret.json` 和 `.log/.pyc/.pyo/.tmp/.key/.pem` 不发布。

## 资产与 README

达到资产阈值的文件会使用 `skill_assets.build_archive` 和 `upload_archive` 归档，并生成 `assets/asset-manifest.json` 与恢复脚本。默认 Git 阈值为 1 MiB；`--asset-mode off` 会在存在大文件时拒绝发布。README 在发布时重新生成；`--readme-file` 可提供自定义 README。

## 退出码

0 成功；2 用法；3 校验（`lint-skill --fail-on-error` 或 `publish-skill` 被门禁拒绝）；4 认证；5 网络；6 Git；7 锁；8 `diff-skill --fail-on-diff` 有差异，或 `lint-skill` 只发现 README 漂移；130 用户中断；1 未预期异常。

## 验证

```powershell
python -m py_compile scripts\skill_git.py scripts\skill_assets.py scripts\skill_lint.py
python -m unittest discover -s tests -v
```

单测覆盖 8 条规则各一个正例一个反例，另含退出码、JSON 契约、审计留档与"发布前置拦截"用例，全部离线运行，不访问网络和真实设备。

资源：`scripts/skill_git.py`、`scripts/skill_assets.py`、`scripts/skill_lint.py`、`references/asset-policy.md`、`requirements.txt`。
