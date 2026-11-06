import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import skill_assets
import skill_git

FAKE_OPENAI = "sk-" + "a" * 24
FAKE_SLACK = "xoxb-" + "a" * 16


class SkillGitTests(unittest.TestCase):
    def skill(self, text="---\nname: demo\ndescription: demo\n---\n# Demo\nBody\n"):
        root = Path(self.tmp.name) / "demo"
        root.mkdir()
        (root / "SKILL.md").write_text(text, encoding="utf-8")
        return root

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_frontmatter_quoted_and_block_scalar(self):
        root = self.skill('---\nname: "demo"\ndescription: |\n  line one\n  line two\n---\n# Demo\n')
        meta = skill_git.parse_skill(root)
        self.assertEqual(meta["name"], "demo")
        self.assertIn("line one", meta["description"])

    def test_name_validation(self):
        with self.assertRaises(skill_git.ValidationError):
            self.skill("---\nname: ../bad\ndescription: x\n---\n").joinpath("SKILL.md")
            skill_git.parse_skill(Path(self.tmp.name) / "demo")

    def test_scan_text_secret_binary_and_exclusions(self):
        root = self.skill()
        (root / "data.json").write_text('{"token":"%s"}' % FAKE_OPENAI, encoding="utf-8")
        (root / "notes.md").write_text(FAKE_SLACK, encoding="utf-8")
        (root / "binary.txt").write_bytes(b"\x00" + FAKE_OPENAI.encode())
        (root / "plan.md").write_text(FAKE_OPENAI, encoding="utf-8")
        (root / ".omo").mkdir(); (root / ".omo" / "x.txt").write_text(FAKE_OPENAI, encoding="utf-8")
        (root / "logs").mkdir(); (root / "logs" / "x.txt").write_text(FAKE_OPENAI, encoding="utf-8")
        scan = skill_assets.scan_skill(root)
        self.assertIn("data.json", scan.secrets)
        self.assertIn("notes.md", scan.secrets)
        self.assertNotIn("binary.txt", scan.secrets)
        self.assertNotIn("plan.md", scan.secrets)

    def test_diff_categories_and_crlf(self):
        left = Path(self.tmp.name) / "left"; right = Path(self.tmp.name) / "right"
        left.mkdir(); right.mkdir()
        (left / "same.txt").write_bytes(b"a\n")
        (right / "same.txt").write_bytes(b"a\r\n")
        (left / "added.txt").write_text("a", encoding="utf-8")
        (right / "deleted.txt").write_text("d", encoding="utf-8")
        (left / "modified.txt").write_text("x", encoding="utf-8")
        (right / "modified.txt").write_text("y", encoding="utf-8")
        diff = skill_git.calculate_diff(left, right)
        self.assertEqual(diff["added"], ["added.txt"])
        self.assertEqual(diff["deleted"], ["deleted.txt"])
        self.assertEqual(diff["modified"], ["modified.txt"])
        self.assertEqual(diff["unchanged"], 1)

    def test_lock_acquire_release_and_stale_recovery(self):
        lock_dir = Path(self.tmp.name) / "locks"
        with mock.patch.dict(os.environ, {"SKILL_GIT_LOCK_DIR": str(lock_dir)}):
            with skill_git.repo_lock("demo"):
                self.assertTrue((lock_dir / "demo.lock").exists())
            self.assertFalse((lock_dir / "demo.lock").exists())
            (lock_dir / "demo.lock").write_text(json.dumps({"pid": 999999, "hostname": "unknown"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SKILL_GIT_LOCK_TIMEOUT": "0"}):
                with skill_git.repo_lock("demo"):
                    self.assertTrue((lock_dir / "demo.lock").exists())

    def test_upload_dry_run_does_not_load_oss(self):
        root = self.skill(); (root / "asset.bin").write_bytes(b"abc")
        args = skill_git.build_parser().parse_args(["upload-assets", "--skill-path", str(root), "--asset", "asset.bin", "--dry-run", "--json"])
        with mock.patch.object(skill_git, "load_oss_sdk", side_effect=AssertionError("OSS called")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output): skill_git.run_upload_assets(args)
        self.assertIn("asset.bin", output.getvalue())

    def test_foreground_scan_ignores_scanner_sources(self):
        root = self.skill()
        (root / "run.ps1").write_text("Start-" + "Process notepad", encoding="utf-8")
        scan = skill_assets.scan_skill(root)
        self.assertIn("run.ps1", scan.foreground_commands)
        repo_root = Path(__file__).resolve().parents[1]
        self.assertNotIn("scripts\\skill_assets.py", skill_assets.scan_skill(repo_root).foreground_commands)

    def test_publish_exclusions_cover_runtime_output_and_local_config(self):
        root = self.skill()
        for rel in ("runtime/log.txt", "output/a.wav", ".venv/x.txt", "secrets/token.txt", "config.local.json", ".env", "deploy.key"):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE_OPENAI, encoding="utf-8")
        files = set(skill_git.publishable_files(root))
        for rel in ("runtime/log.txt", "output/a.wav", ".venv/x.txt", "secrets/token.txt", "config.local.json", ".env", "deploy.key"):
            self.assertNotIn(rel, files)
        scan = skill_assets.scan_skill(root)
        self.assertFalse(any(p.startswith(("runtime/", "output/", ".venv/", "secrets/")) for p in scan.secrets))

    def test_ensure_local_repo_updates_from_origin(self):
        repo_dir = Path(self.tmp.name) / "cache"
        calls = []

        class Result:
            returncode = 0
            stderr = ""
            def __init__(self, stdout=""):
                self.stdout = stdout

        def fake_run(command, cwd=None, check=True, env=None):
            calls.append(command)
            if command[:3] == ["git", "ls-remote", "--heads"]:
                return Result("refs/heads/main\n")
            return Result()

        with mock.patch.object(skill_git, "run_command", side_effect=fake_run):
            skill_git.ensure_local_repo(repo_dir, "owner", "repo", "main", "host", "ssh", "token")
        self.assertIn(["git", "checkout", "-B", "main", "origin/main"], calls)
        self.assertIn(["git", "pull", "--ff-only", "origin", "main"], calls)

    def test_https_askpass_writes_token_helper(self):
        with skill_git.git_auth_env("https", "secret-token") as env:
            text = Path(env["GIT_ASKPASS"]).read_text(encoding="ascii")
            self.assertIn("SKILL_GIT_ASKPASS_TOKEN", text)
            self.assertIn("x-access-token", text)

    def test_error_exit_mapping(self):
        for error, code in ((skill_git.UsageError("x"), 2), (skill_git.ValidationError("x"), 3), (skill_git.AuthError("x"), 4), (skill_git.NetworkError("x"), 5), (skill_git.GitError("x"), 6), (skill_git.LockError("x"), 7)):
            self.assertEqual(error.exit_code, code)


if __name__ == "__main__":
    unittest.main()
