# Git / OSS Asset Policy

本文是 `skill-git` 发布时的资产边界。目标不是“Git 能推多大就推多大”，而是让仓库可克隆、可审阅，并让受限客户端也能导入。

## 阈值

| 范围 | 处理 |
|---|---|
| 小于 1 MiB 的源码、配置、文档、小型静态资源 | 留在 Git |
| 1 MiB 及以上，或二进制/压缩包/固件/模型/媒体 | 打包到 OSS，不进入 Git |
| 50 MiB 及以上 | GitHub 仍可能接受，但必须视为高风险；不要提交 |
| 100 MiB 及以上 | GitHub 普通 Git 对象硬限制，禁止提交 |

这里的 **1 MiB** 是 Skill 分发阈值，用于兼容带单文件 1 MiB 限制的导入器；GitHub 官方普通 Git 的限制更宽，但不能因此把 Skill 做成大文件仓库。GitHub 官方说明：50 MiB 警告、100 MiB 阻止，仓库建议小于 1 GB、强烈建议小于 5 GB。参考：

- https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github
- https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits

## 永远不进入 Git

- `plan.md`（大小写不敏感、任意层级）和 `.omo/`，它们是本地计划文件
- `result/`、`multi_logs/`、`logs/`、串口原始日志
- `.opencode/`、`.git/`、`__pycache__/`、`.pytest_cache/`、`node_modules/`、`.pyc`、临时文件
- 运行生成的测试报告、截图、录音、录屏
- 可重新生成的构建中间件：`.asm`、`.lst`、`.map`、`.elf` 等
- 密钥、token、AccessKey、私钥、证书私钥
- 缓存、虚拟环境和 node_modules

这些内容即使体积不大也不应该被当作源码发布；日志和测试证据留在本地或测试结果目录。

## 应放 OSS

- 固件：`.hex`、`.img`、`.bin`、`.fwpkg`
- 厂商工具：`.exe`、`.zip`、驱动包
- 模型、音频库、图片/视频、数据集、数据库导出
- 任何超过阈值的压缩包、二进制、构建产物

OSS 对象必须有不可变版本路径、SHA-256 和大小。仓库只保留一个小的 `assets/asset-manifest.json` 和恢复脚本，禁止使用 `latest/` 这类可变地址。

## `skill-git` 发布流程

```text
扫描 Skill
  -> 发现 >1MiB 文件
  -> 打包为 zip + asset-manifest.json
  -> 上传 OSS（public-read）
  -> 从 Git 副本排除这些文件
  -> 写入 assets/asset-manifest.json
  -> 写入 scripts/ensure_assets.py（若目标没有自定义实现）
  -> commit / push
```

其他节点克隆后执行：

```powershell
python -u scripts/ensure_assets.py
```

恢复脚本只下载缺失资产，校验压缩包和每个文件的 SHA-256，不覆盖已有文件。

## 环境变量

```text
GITHUB_TOKEN
ACCESS_KEY_ID
ACCESS_KEY_SECRET
```

可选 OSS 覆盖：

```text
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_REGION=cn-beijing
OSS_BUCKET=listenai-test-internal
OSS_PREFIX=exe/bszheng/
OSS_ACL=public-read
```

不要把任何密钥写入 `SKILL.md`、脚本、Git remote、README 或日志。其他节点通过环境变量配置后使用。
