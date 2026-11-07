"""Settings, environment loading and the command line. No network, no credentials."""
import asyncio
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_migration import ConfigError, Settings, cli, config, runner

ROOT = Path(__file__).resolve().parents[1]
GOOD_ENV = {
    "TELEGRAM_API_ID": "424242", "TELEGRAM_API_HASH": "abcdef", "TELEGRAM_BOT_TOKEN": "1:token",
    "TELEGRAM_CHANNEL_PEER": "-1001234567890", "TELEGRAM_EXPORT_DIR": "/data/export",
}


class SettingsTests(unittest.TestCase):
    def test_limits_follow_telegram_and_cannot_exceed_its_maximums(self):
        self.assertEqual((Settings().text_limit, Settings().caption_limit), (4096, 1024))
        for field_name, value in (("text_limit", 4097), ("text_limit", 0), ("caption_limit", 2049), ("caption_limit", 0)):
            with self.assertRaises(ConfigError, msg=f"{field_name}={value}") as caught:
                Settings.from_env(GOOD_ENV, **{field_name: value}).validate()
            self.assertIn(field_name, str(caught.exception))
        Settings.from_env(GOOD_ENV, text_limit=1000, caption_limit=900).validate()

    def test_text_limit_below_4_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            Settings.from_env(GOOD_ENV, text_limit=3).validate()
        self.assertIn("text_limit", str(caught.exception))
        self.assertIn("4", str(caught.exception))

    def test_caption_limit_below_4_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            Settings.from_env(GOOD_ENV, caption_limit=3).validate()
        self.assertIn("caption_limit", str(caught.exception))
        self.assertIn("4", str(caught.exception))

    def test_the_settings_proxy_is_read_only_so_patching_cannot_corrupt_it(self):
        before = config.get_settings().idmap_file
        with self.assertRaises(AttributeError):
            config.settings.idmap_file = "/elsewhere"
        with self.assertRaises(AttributeError):
            patch.object(config.settings, "idmap_file", "/elsewhere").start()
        self.assertEqual(config.get_settings().idmap_file, before)

    def test_from_env_reads_the_documented_variables(self):
        s = Settings.from_env(GOOD_ENV)
        self.assertEqual((s.api_id, s.api_hash, s.bot_token), (424242, "abcdef", "1:token"))
        self.assertEqual(s.channel_peer, -1001234567890)
        self.assertEqual(s.base_dir, os.path.abspath("/data/export"))
        self.assertTrue(s.dry_run)
        self.assertFalse(s.skip_confirmation)
        self.assertTrue(s.preserve_service_messages)

    def test_service_message_setting_can_be_disabled_from_environment(self):
        s = Settings.from_env({**GOOD_ENV, "TELEGRAM_PRESERVE_SERVICE_MESSAGES": "false"})
        self.assertFalse(s.preserve_service_messages)

    def test_channel_peer_may_be_a_username(self):
        self.assertEqual(Settings.from_env({**GOOD_ENV, "TELEGRAM_CHANNEL_PEER": "@my_channel"}).channel_peer, "@my_channel")

    def test_dry_run_and_optional_numbers_come_from_the_environment(self):
        s = Settings.from_env({**GOOD_ENV, "TELEGRAM_DRY_RUN": "false", "TELEGRAM_SKIP_CONFIRMATION": "yes",
                               "TELEGRAM_MAX_UPLOAD_MB": "50", "TELEGRAM_BATCH_LIMIT": "10",
                               "TELEGRAM_SEND_DELAY": "0.5"})
        self.assertFalse(s.dry_run)
        self.assertTrue(s.skip_confirmation)
        self.assertEqual((s.max_upload_mb, s.batch_limit, s.send_delay_seconds), (50.0, 10, 0.5))

    def test_zero_or_none_disables_the_upload_check(self):
        for raw in ("0", "none", "off"):
            self.assertIsNone(Settings.from_env({**GOOD_ENV, "TELEGRAM_MAX_UPLOAD_MB": raw}).max_upload_mb)

    def test_bad_numbers_are_reported_clearly(self):
        for name in ("TELEGRAM_API_ID", "TELEGRAM_MAX_UPLOAD_MB", "TELEGRAM_BATCH_LIMIT"):
            with self.assertRaises(ConfigError, msg=name):
                Settings.from_env({**GOOD_ENV, name: "many"})

    def test_explicit_keyword_arguments_win_over_the_environment(self):
        self.assertFalse(Settings.from_env(GOOD_ENV, dry_run=False).dry_run)

    def test_progress_files_live_in_the_export_folder(self):
        s = Settings(base_dir="/data/export")
        self.assertEqual(s.idmap_file, os.path.join(os.path.abspath("/data/export"), "migration_id_map.jsonl"))
        self.assertEqual(s.manual_uploads_file, os.path.join(os.path.abspath("/data/export"), "migration_manual_uploads.txt"))

    def test_explicit_file_paths_are_respected_and_replace_moves_the_rest(self):
        s = Settings(base_dir="/a", failed_file="/elsewhere/failed.txt")
        self.assertEqual(s.failed_file, "/elsewhere/failed.txt")
        moved = s.replace(base_dir="/b")
        self.assertTrue(moved.idmap_file.startswith(os.path.abspath("/b")))

    def test_secrets_are_not_shown_in_repr(self):
        text = repr(Settings(api_hash="SECRET-HASH", bot_token="SECRET-TOKEN"))
        self.assertNotIn("SECRET-HASH", text)
        self.assertNotIn("SECRET-TOKEN", text)

    def test_validate_lists_everything_that_is_missing(self):
        with self.assertRaises(ConfigError) as caught:
            Settings().validate()
        for fragment in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_PEER"):
            self.assertIn(fragment, str(caught.exception))

    def test_validate_rejects_the_example_values(self):
        example = Settings(api_id=config.PLACEHOLDER_API_ID, api_hash="replace_with_your_api_hash",
                           bot_token="replace_with_your_new_bot_token", channel_peer=-1001234567890)
        with self.assertRaises(ConfigError) as caught:
            example.validate()
        self.assertEqual(str(caught.exception).count("example value"), 3)

    def test_validate_accepts_real_looking_settings(self):
        self.assertIsInstance(Settings.from_env(GOOD_ENV).validate(), Settings)

    def test_use_settings_restores_the_previous_settings(self):
        before = config.get_settings()
        temporary = Settings(batch_limit=7)
        with config.use_settings(temporary):
            self.assertIs(config.get_settings(), temporary)
        self.assertIs(config.get_settings(), before)

    def test_use_settings_is_isolated_between_async_tasks(self):
        async def read_after_yield(settings):
            with config.use_settings(settings):
                await asyncio.sleep(0)
                return config.get_settings().batch_limit

        async def scenario():
            return await asyncio.gather(
                read_after_yield(Settings(batch_limit=3)),
                read_after_yield(Settings(batch_limit=9)),
            )

        self.assertEqual(asyncio.run(scenario()), [3, 9])

    def test_resolve_settings_applies_overrides(self):
        base = Settings(batch_limit=1)
        self.assertEqual(config.resolve_settings(base, batch_limit=9).batch_limit, 9)
        self.assertIs(config.resolve_settings(base), base)

    def test_runner_validates_settings_before_starting(self):
        with self.assertRaises(ConfigError):
            runner.run(Settings())


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--env-file", os.devnull, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_missing_configuration_exits_with_status_2_and_a_clear_message(self):
        code, _, err = self.run_cli([], {})
        self.assertEqual(code, 2)
        self.assertIn("Configuration error", err)
        self.assertIn("TELEGRAM_API_HASH", err)

    def test_flags_override_the_environment_and_are_passed_to_run(self):
        seen = {}
        with patch.object(runner, "run", lambda settings: seen.setdefault("settings", settings)):
            code, _, _ = self.run_cli(["--send", "--yes", "--export-dir", "/tmp/exp", "--channel", "@dest",
                                       "--max-upload-mb", "50", "--batch-limit", "5", "--delay", "0"], GOOD_ENV)
        s = seen["settings"]
        self.assertEqual(code, 0)
        self.assertFalse(s.dry_run)
        self.assertTrue(s.skip_confirmation)
        self.assertEqual((s.channel_peer, s.max_upload_mb, s.batch_limit, s.send_delay_seconds), ("@dest", 50.0, 5, 0.0))
        self.assertEqual(s.base_dir, os.path.abspath("/tmp/exp"))

    def test_no_service_messages_flag_overrides_environment(self):
        seen = {}
        with patch.object(runner, "run", lambda settings: seen.setdefault("settings", settings)):
            code, _, _ = self.run_cli(["--no-service-messages"], GOOD_ENV)
        self.assertEqual(code, 0)
        self.assertFalse(seen["settings"].preserve_service_messages)

    def test_dry_run_is_the_default(self):
        seen = {}
        with patch.object(runner, "run", lambda settings: seen.setdefault("settings", settings)):
            self.run_cli([], GOOD_ENV)
        self.assertTrue(seen["settings"].dry_run)

    def test_numeric_channel_flag_becomes_a_number(self):
        seen = {}
        with patch.object(runner, "run", lambda settings: seen.setdefault("settings", settings)):
            self.run_cli(["--channel", "-100999"], {k: v for k, v in GOOD_ENV.items() if k != "TELEGRAM_CHANNEL_PEER"})
        self.assertEqual(seen["settings"].channel_peer, -100999)

    def test_zero_max_upload_flag_disables_the_check(self):
        seen = {}
        with patch.object(runner, "run", lambda settings: seen.setdefault("settings", settings)):
            self.run_cli(["--max-upload-mb", "0"], GOOD_ENV)
        self.assertIsNone(seen["settings"].max_upload_mb)

    def test_ctrl_c_exits_with_130_and_says_progress_is_saved(self):
        def interrupted(settings):
            raise KeyboardInterrupt
        with patch.object(runner, "run", interrupted):
            code, _, err = self.run_cli([], GOOD_ENV)
        self.assertEqual(code, 130)
        self.assertIn("Progress made so far is saved", err)

    def test_run_returning_false_exits_with_status_1(self):
        """run() returning False must become exit code 1 — a start failure."""
        with patch.object(runner, "run", lambda settings: False):
            code, _, _ = self.run_cli([], GOOD_ENV)
        self.assertEqual(code, 1)

    def test_run_returning_true_exits_with_status_0(self):
        with patch.object(runner, "run", lambda settings: True):
            code, _, _ = self.run_cli([], GOOD_ENV)
        self.assertEqual(code, 0)

    def test_version_flag_prints_the_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cli.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("telegram-migration", out.getvalue())


class PackageTests(unittest.TestCase):
    def python(self, *args):
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=ROOT,
                              env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)

    def test_importing_the_package_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(
                [sys.executable, "-c", "import sys, telegram_migration as t; print(t.__version__); "
                 "print('telethon' in sys.modules)"],
                capture_output=True, text=True, cwd=empty, env={**os.environ, "PYTHONPATH": str(ROOT)},
                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        version, telethon_loaded = result.stdout.split()
        self.assertEqual(telethon_loaded, "False")
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_python_dash_m_help_works(self):
        result = self.python("-m", "telegram_migration", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--send", result.stdout)


if __name__ == "__main__":
    unittest.main()
