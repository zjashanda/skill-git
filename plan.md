# plan

- [completed] Bootstrap: read `SKILL.md`, assess repo-visibility task, and confirm available GitHub auth/config.
- [completed] Re-check the current visibility of `zjashanda/mars-belt`, then switch it to public if it is still private.
- [completed] Verify anonymous public access and sync the final result back into this file.

## result

- Repository: `zjashanda/mars-belt`
- Verified at: `2026-04-22` Asia/Shanghai
- Authenticated GitHub API check: `200`, `private=false`, `visibility=public`
- Anonymous GitHub API check: `200`, public access confirmed
- Public URL: `https://github.com/zjashanda/mars-belt`

## skill-git publisher update (2026-11-06)

- [completed] Added Git/OSS asset policy: 1 MiB skill threshold, GitHub 50/100 MiB external limits, 1/5 GB repository guidance.
- [completed] Added `scripts/skill_assets.py`: large-file scan, OSS ZIP/manifest upload, generated `scripts/ensure_assets.py` bootstrap.
- [completed] Integrated automatic OSS handling into `publish-skill`, including large-file Git exclusion and asset-count commit messages.
- [completed] Added SSH/HTTPS transport selection, token askpass support, secret scanning, foreground-terminal checks, and layout/environment warnings.
- [completed] Added `requirements.txt`, `references/asset-policy.md`, and dual-client install copies under `.codex` and `.config/opencode`.
- [completed] Verified Python compilation, publish help options, OSS module upload/public download/delete, transport probing, and asset bootstrap generation.
- [completed] Live GitHub push completed on 2026-11-06; remote `zjashanda/skill-git` main was updated from local `skill-git` via SSH.
