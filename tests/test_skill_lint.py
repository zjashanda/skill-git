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

import skill_git
import skill_lint

DESCRIPTION = "demo skill for lint tests"
BODY = "# Demo\n\nRun the demo workflow and report the result.\n"


class LintTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.registry_path = self.base / "registry" / "skills.json"
        self.audit_dir = self.base / "audit"
        self.cache_dir = self.base / "cache"
        self.cache_dir.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "SKILL_GIT_CACHE_DIR": str(self.cache_dir),
                "SKILL_GIT_AUDIT_DIR": str(self.audit_dir),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def make_skill(self, name="demo", directory=None, body=BODY, description=DESCRIPTION):
        root = self.base / (directory or name)
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
            encoding="utf-8",
        )
        return root

    def write_readme(self, root):
        meta = skill_git.parse_skill(root)
        (root / "README.md").write_text(skill_git.build_skill_readme(meta, root), encoding="utf-8")
        return root / "README.md"

    def write_registry(self, entries, shape="skills-list"):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = entries
        if shape == "skills-list":
            payload = {"schema_version": 1, "skills": entries}
        elif shape == "skills-dict":
            payload = {"schema_version": 1, "skills": {e["name"]: e for e in entries}}
        self.registry_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.registry_path

    def entry(self, name="demo", **overrides):
        data = {
            "name": name,
            "repo": f"zjashanda/{name}",
            "status": "active",
            "reuse": "medium",
            "scope_note": "only for the demo project",
        }
        data.update(overrides)
        return data

    def compliant(self, name="demo"):
        root = self.make_skill(name)
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        self.write_readme(root)
        self.write_registry([self.entry(name)])
        return root

    def lint(self, root, **kwargs):
        kwargs.setdefault("registry_path", self.registry_path)
        return skill_lint.lint_skill(root, **kwargs)

    def rules(self, report, level=None):
        return {item.rule for item in report.violations if level is None or item.level == level}


class FrontmatterRuleTests(LintTestBase):
    def test_valid_frontmatter_passes(self):
        report = self.lint(self.compliant())
        self.assertNotIn("frontmatter", self.rules(report))

    def test_name_must_match_repository_name(self):
        root = self.make_skill("other", directory="demo")
        self.write_readme(root)
        self.write_registry([self.entry("other")])
        report = self.lint(root)
        self.assertIn("frontmatter", self.rules(report, "error"))

    def test_invalid_name_is_rejected(self):
        root = self.make_skill("../evil", directory="evil")
        report = self.lint(root)
        self.assertIn("frontmatter", self.rules(report, "error"))

    def test_missing_skill_md_is_rejected(self):
        root = self.base / "demo"
        root.mkdir()
        report = self.lint(root)
        self.assertIn("frontmatter", self.rules(report, "error"))


class ExcludedRuleTests(LintTestBase):
    def test_clean_tree_has_no_excluded_hits(self):
        report = self.lint(self.compliant())
        self.assertNotIn("excluded", self.rules(report))

    def test_venv_and_plan_md_are_rejected(self):
        root = self.compliant()
        (root / ".venv" / "Lib").mkdir(parents=True)
        (root / ".venv" / "Lib" / "x.py").write_text("x = 1\n", encoding="utf-8")
        (root / "plan.md").write_text("local plan\n", encoding="utf-8")
        report = self.lint(root)
        hits = {item.rule: item.paths for item in report.violations}
        self.assertIn("excluded", hits)
        joined = " ".join(hits["excluded"])
        self.assertIn(".venv/", joined)
        self.assertIn("plan.md", joined)

    def test_nested_git_and_secrets_are_rejected(self):
        root = self.compliant()
        (root / "secrets").mkdir()
        (root / "secrets" / "token.txt").write_text("x\n", encoding="utf-8")
        (root / "nested" / ".git").mkdir(parents=True)
        (root / "server.pem").write_text("x\n", encoding="utf-8")
        report = self.lint(root)
        joined = " ".join(item.paths and " ".join(item.paths) or "" for item in report.violations if item.rule == "excluded")
        for expected in ("secrets/", "nested/.git/", "server.pem"):
            self.assertIn(expected, joined)


class SizeRuleTests(LintTestBase):
    def test_small_repository_passes(self):
        report = self.lint(self.compliant(), max_bytes=1024 * 1024)
        self.assertNotIn("size", self.rules(report))

    def test_over_limit_is_rejected(self):
        root = self.compliant()
        (root / "assets").mkdir()
        (root / "assets" / "blob.bin").write_bytes(b"0" * 4096)
        report = self.lint(root, max_bytes=1024)
        self.assertIn("size", self.rules(report, "error"))

    def test_allow_large_downgrades_to_warning(self):
        root = self.compliant()
        (root / "assets").mkdir()
        (root / "assets" / "blob.bin").write_bytes(b"0" * 4096)
        report = self.lint(root, max_bytes=1024, allow_large=True)
        self.assertIn("size", self.rules(report, "warning"))
        self.assertNotIn("size", self.rules(report, "error"))

    def test_excluded_content_counts_towards_the_limit(self):
        root = self.compliant()
        (root / ".venv" / "lib").mkdir(parents=True)
        (root / ".venv" / "lib" / "big.bin").write_bytes(b"0" * 8192)
        report = self.lint(root, max_bytes=1024)
        self.assertIn("size", self.rules(report, "error"))


class EmbeddedCopyRuleTests(LintTestBase):
    def test_normal_subdirectories_pass(self):
        report = self.lint(self.compliant())
        self.assertNotIn("embedded", self.rules(report))

    def test_nested_skill_md_is_rejected(self):
        root = self.compliant()
        (root / "copied").mkdir()
        (root / "copied" / "SKILL.md").write_text("---\nname: copied\n---\n", encoding="utf-8")
        report = self.lint(root, readme_level="warning")
        self.assertIn("embedded", self.rules(report, "error"))

    def test_known_skill_directory_without_skill_md_is_rejected(self):
        root = self.compliant()
        (root / "lstest" / "scripts").mkdir(parents=True)
        (root / "lstest" / "scripts" / "core.py").write_text("x = 1\n", encoding="utf-8")
        report = self.lint(root, known_skills=["lstest"], readme_level="warning")
        self.assertIn("embedded", self.rules(report, "error"))

    def test_unknown_directory_name_is_not_flagged(self):
        root = self.compliant()
        (root / "tools" / "scripts").mkdir(parents=True)
        (root / "tools" / "scripts" / "core.py").write_text("x = 1\n", encoding="utf-8")
        report = self.lint(root, known_skills=["lstest"], readme_level="warning")
        self.assertNotIn("embedded", self.rules(report))


class DependenciesRuleTests(LintTestBase):
    def test_requirements_file_passes(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        self.write_readme(root)
        report = self.lint(root)
        self.assertNotIn("dependencies", self.rules(report))

    def test_python_scripts_without_declaration_are_rejected(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
        self.write_readme(root)
        report = self.lint(root)
        self.assertIn("dependencies", self.rules(report, "error"))

    def test_non_python_scripts_need_no_declaration(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "run.ps1").write_text("Write-Host ok\n", encoding="utf-8")
        self.write_readme(root)
        report = self.lint(root)
        self.assertNotIn("dependencies", self.rules(report))


class ReadmeRuleTests(LintTestBase):
    def test_generated_readme_matches(self):
        report = self.lint(self.compliant())
        self.assertNotIn("readme", self.rules(report))

    def test_missing_readme_is_a_warning(self):
        root = self.make_skill()
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        self.write_registry([self.entry()])
        report = self.lint(root)
        self.assertIn("readme", self.rules(report, "warning"))
        self.assertEqual(report.exit_code(True), 0)

    def test_layout_drift_is_rejected(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "new.py").write_text("print('x')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        report = self.lint(root)
        self.assertIn("readme", self.rules(report, "error"))
        self.assertEqual(report.exit_code(True), 8)

    def test_body_drift_is_rejected(self):
        root = self.compliant()
        (root / "SKILL.md").write_text(
            "---\nname: demo\ndescription: %s\n---\n\n%s\n## New section\n\nchanged\n" % (DESCRIPTION, BODY),
            encoding="utf-8",
        )
        report = self.lint(root)
        self.assertIn("readme", self.rules(report, "error"))

    def test_handwritten_readme_is_skipped_with_a_warning(self):
        root = self.make_skill()
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        (root / "README.md").write_text("# demo\n\n手写说明，没有生成段落。\n", encoding="utf-8")
        self.write_registry([self.entry()])
        report = self.lint(root)
        self.assertIn("readme", self.rules(report, "warning"))
        self.assertNotIn("readme", self.rules(report, "error"))
        self.assertEqual(report.exit_code(True), 0)

    def test_extra_handwritten_sections_are_allowed(self):
        root = self.compliant()
        readme = root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\n## 手写补充\n\n补充说明\n",
            encoding="utf-8",
        )
        report = self.lint(root)
        self.assertNotIn("readme", self.rules(report, "error"))

    def test_publish_treats_drift_as_warning(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "new.py").write_text("print('x')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        report = self.lint(root, readme_level="warning")
        self.assertNotIn("readme", self.rules(report, "error"))
        self.assertEqual(report.exit_code(True), 0)


class RegistryRuleTests(LintTestBase):
    def test_registered_skill_passes(self):
        report = self.lint(self.compliant())
        self.assertNotIn("registry", self.rules(report))

    def test_unregistered_skill_is_rejected(self):
        root = self.compliant()
        self.write_registry([self.entry("other")])
        report = self.lint(root)
        self.assertIn("registry", self.rules(report, "error"))

    def test_archived_skill_is_rejected(self):
        root = self.make_skill()
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        self.write_readme(root)
        self.write_registry([self.entry(status="archived")])
        report = self.lint(root)
        self.assertIn("registry", self.rules(report, "error"))

    def test_empty_reuse_and_scope_note_are_rejected(self):
        root = self.make_skill()
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        self.write_readme(root)
        self.write_registry([self.entry(reuse="", scope_note="")])
        report = self.lint(root)
        messages = " ".join(item.message for item in report.violations if item.rule == "registry")
        self.assertIn("reuse", messages)
        self.assertIn("scope_note", messages)

    def test_missing_registry_degrades_to_warning(self):
        root = self.compliant()
        self.registry_path.unlink()
        report = self.lint(root)
        self.assertIn("registry", self.rules(report, "warning"))
        self.assertNotIn("registry", self.rules(report, "error"))
        self.assertEqual(report.exit_code(True), 0)
        self.assertEqual(report.stats["registry_state"], "missing")

    def test_unreadable_registry_is_an_error(self):
        root = self.compliant()
        self.registry_path.write_text("{not json", encoding="utf-8")
        report = self.lint(root)
        self.assertIn("registry", self.rules(report, "error"))

    def test_registry_shapes_are_tolerated(self):
        root = self.make_skill()
        (root / "references").mkdir()
        (root / "references" / "index.md").write_text("# Index\n", encoding="utf-8")
        self.write_readme(root)
        for shape in ("skills-list", "skills-dict", "bare-list"):
            with self.subTest(shape=shape):
                self.write_registry([self.entry()], shape=shape)
                report = self.lint(root)
                self.assertNotIn("registry", self.rules(report, "error"))

    def test_registry_env_var_is_used(self):
        root = self.compliant()
        with mock.patch.dict(os.environ, {"SKILL_GIT_REGISTRY": str(self.registry_path)}):
            report = skill_lint.lint_skill(root)
        self.assertNotIn("registry", self.rules(report, "error"))


class GitAwareRuleTests(LintTestBase):
    def git(self, root, *args):
        import subprocess

        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    def test_gitignored_pycache_is_not_a_violation(self):
        root = self.compliant()
        self.git(root, "init")
        (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (root / "scripts").mkdir()
        (root / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        (root / "scripts" / "__pycache__").mkdir()
        (root / "scripts" / "__pycache__" / "run.pyc").write_bytes(b"\x00")
        self.write_readme(root)
        report = self.lint(root)
        self.assertNotIn("excluded", self.rules(report))

    def test_untracked_but_not_ignored_venv_is_rejected(self):
        root = self.compliant()
        self.git(root, "init")
        (root / ".venv").mkdir()
        (root / ".venv" / "x.py").write_text("x = 1\n", encoding="utf-8")
        report = self.lint(root)
        self.assertIn("excluded", self.rules(report, "error"))

    def test_tracked_venv_is_rejected(self):
        root = self.compliant()
        self.git(root, "init")
        (root / ".venv").mkdir()
        (root / ".venv" / "x.py").write_text("x = 1\n", encoding="utf-8")
        self.git(root, "add", ".venv/x.py")
        report = self.lint(root)
        self.assertIn("excluded", self.rules(report, "error"))


class AuditTests(LintTestBase):
    def run_cli(self, argv):
        args = skill_git.build_parser().parse_args(argv)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = args.func(args)
        return code, output.getvalue()

    def test_audit_record_contains_actor_files_and_verdict(self):
        root = self.compliant()
        with mock.patch.dict(os.environ, {"SKILL_GIT_ACTOR": "tester@ci"}):
            code, output = self.run_cli(
                [
                    "lint-skill",
                    "--skill-path", str(root),
                    "--registry", str(self.registry_path),
                    "--audit",
                    "--audit-dir", str(self.audit_dir),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        record_path = Path(payload["audit_path"])
        self.assertTrue(record_path.is_file())
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["actor"], "tester@ci")
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["command"], "lint-skill")
        self.assertIn("files_checked", record["stats"])
        self.assertTrue(record["timestamp"].endswith("Z"))

    def test_failed_lint_audit_keeps_the_rejection(self):
        root = self.compliant()
        (root / ".venv").mkdir()
        (root / ".venv" / "x.txt").write_text("x\n", encoding="utf-8")
        code, output = self.run_cli(
            [
                "lint-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--audit",
                "--audit-dir", str(self.audit_dir),
                "--fail-on-error",
                "--json",
            ]
        )
        self.assertEqual(code, 3)
        record = json.loads(Path(json.loads(output)["audit_path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["verdict"], "fail")
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("excluded", {item["rule"] for item in record["violations"]})

    def test_dry_run_writes_no_audit_record(self):
        root = self.compliant()
        code, output = self.run_cli(
            [
                "lint-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--audit",
                "--dry-run",
                "--audit-dir", str(self.audit_dir),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertFalse(self.audit_dir.exists() and any(self.audit_dir.iterdir()))
        self.assertIsNone(json.loads(output)["audit_path"])

    def test_audit_write_failure_does_not_hide_the_verdict(self):
        root = self.compliant()
        blocked = self.base / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        code, output = self.run_cli(
            [
                "lint-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--audit",
                "--audit-dir", str(blocked),
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        rules = {item["rule"] for item in json.loads(output)["violations"]}
        self.assertIn("audit", rules)


class CliContractTests(LintTestBase):
    def run_cli(self, argv):
        args = skill_git.build_parser().parse_args(argv)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = args.func(args)
        return code, output.getvalue()

    def test_reports_without_failing_by_default(self):
        root = self.compliant()
        (root / ".venv").mkdir()
        (root / ".venv" / "x.txt").write_text("x\n", encoding="utf-8")
        code, output = self.run_cli(
            ["lint-skill", "--skill-path", str(root), "--registry", str(self.registry_path)]
        )
        self.assertEqual(code, 0)
        self.assertIn("verdict: fail", output)

    def test_fail_on_error_returns_3(self):
        root = self.compliant()
        (root / ".venv").mkdir()
        (root / ".venv" / "x.txt").write_text("x\n", encoding="utf-8")
        code, _ = self.run_cli(
            [
                "lint-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--fail-on-error",
            ]
        )
        self.assertEqual(code, 3)

    def test_readme_drift_returns_8(self):
        root = self.compliant()
        (root / "scripts").mkdir()
        (root / "scripts" / "new.py").write_text("print('x')\n", encoding="utf-8")
        (root / "requirements.txt").write_text("PyYAML>=6.0\n", encoding="utf-8")
        code, _ = self.run_cli(
            [
                "lint-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--fail-on-error",
            ]
        )
        self.assertEqual(code, 8)

    def test_json_output_is_a_single_object(self):
        root = self.compliant()
        code, output = self.run_cli(
            ["lint-skill", "--skill-path", str(root), "--registry", str(self.registry_path), "--json"]
        )
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["verdict"], "pass")
        self.assertEqual(payload["skill"], "demo")
        self.assertIn("stats", payload)

    def test_missing_skill_argument_is_a_usage_error(self):
        args = skill_git.build_parser().parse_args(["lint-skill", "--registry", str(self.registry_path)])
        with self.assertRaises(skill_git.UsageError):
            args.func(args)


class PublishGateTests(LintTestBase):
    def publish_args(self, root, *extra):
        return skill_git.build_parser().parse_args(
            [
                "publish-skill",
                "--skill-path", str(root),
                "--registry", str(self.registry_path),
                "--audit-dir", str(self.audit_dir),
                *extra,
            ]
        )

    def test_publish_dry_run_stops_before_any_remote_action(self):
        root = self.compliant()
        (root / ".venv").mkdir()
        (root / ".venv" / "x.txt").write_text("x\n", encoding="utf-8")
        args = self.publish_args(root, "--dry-run")
        with mock.patch.object(skill_git, "resolve_token", side_effect=AssertionError("token requested")), \
             mock.patch.object(skill_git, "ensure_remote_repo", side_effect=AssertionError("remote touched")), \
             mock.patch.object(skill_git, "ensure_local_repo", side_effect=AssertionError("clone attempted")), \
             mock.patch.object(skill_git, "select_transport", side_effect=AssertionError("transport probed")), \
             mock.patch.object(skill_git, "repo_lock", side_effect=AssertionError("lock taken")):
            with self.assertRaises(skill_git.ValidationError) as context:
                skill_git.run_publish_skill(args)
        self.assertIn("lint-skill rejected demo", str(context.exception))

    def test_rejected_publish_still_leaves_an_audit_record(self):
        root = self.compliant()
        (root / ".venv").mkdir()
        (root / ".venv" / "x.txt").write_text("x\n", encoding="utf-8")
        args = self.publish_args(root, "--dry-run")
        with self.assertRaises(skill_git.ValidationError):
            skill_git.run_publish_skill(args)
        records = sorted(self.audit_dir.glob("*-demo.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["command"], "publish-skill")
        self.assertEqual(record["verdict"], "fail")

    def test_publish_dry_run_reports_lint_verdict(self):
        root = self.compliant()
        args = self.publish_args(root, "--dry-run", "--json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = skill_git.run_publish_skill(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["lint"]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
