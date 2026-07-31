import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import postout.cli as cli


class EmailValidationTests(unittest.TestCase):
    def test_accepts_normal_email(self):
        self.assertTrue(
            cli.is_reasonable_email("user@example.com")
        )

    def test_accepts_internal_domain(self):
        self.assertTrue(
            cli.is_reasonable_email("alerts@mailhost")
        )

    def test_rejects_missing_at_sign(self):
        self.assertFalse(
            cli.is_reasonable_email("not-an-email")
        )

    def test_rejects_line_break(self):
        self.assertFalse(
            cli.is_reasonable_email(
                "user@example.com\nBcc: attacker@example.com"
            )
        )


class RecipientTests(unittest.TestCase):
    def test_parses_quoted_commas_and_multiple_recipients(self):
        recipients = cli.parse_recipients(
            '"Doe, Jane" <jane@example.com>, bob@example.com',
            "To",
        )

        self.assertEqual(
            [address for _, address in recipients],
            [
                "jane@example.com",
                "bob@example.com",
            ],
        )

    def test_deduplicates_envelope_addresses_case_insensitively(self):
        addresses = cli.deduplicate_envelope_addresses(
            [
                ("Alice <alice@example.com>", "alice@example.com"),
                ("ALICE@example.com", "ALICE@example.com"),
            ],
            [
                ("Bob <bob@example.com>", "bob@example.com"),
            ],
        )

        self.assertEqual(
            addresses,
            [
                "alice@example.com",
                "bob@example.com",
            ],
        )

    def test_rejects_recipient_header_injection(self):
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as context,
        ):
            cli.parse_recipients(
                "user@example.com\nBcc: attacker@example.com",
                "To",
            )

        self.assertEqual(context.exception.code, 2)


class MessageTests(unittest.TestCase):
    def test_html_to_text_generates_readable_fallback(self):
        result = cli.html_to_text(
            "<h1>Status</h1><p>All checks passed.</p>"
        )

        self.assertIn("Status", result)
        self.assertIn("All checks passed.", result)

    def test_builds_formatted_from_header(self):
        result = cli.build_from_header(
            "Postout Alerts",
            "alerts@example.com",
        )

        self.assertEqual(
            result,
            "Postout Alerts <alerts@example.com>",
        )

    def test_reads_body_argument(self):
        args = SimpleNamespace(
            body="Hello from Postout",
            body_file=None,
        )

        self.assertEqual(
            cli.read_body(args),
            "Hello from Postout",
        )


class AttachmentTests(unittest.TestCase):
    def test_accepts_regular_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "report.txt"
            attachment.write_text(
                "report",
                encoding="utf-8",
            )

            self.assertEqual(
                cli.preflight_attachments(
                    [str(attachment)]
                ),
                6,
            )

    def test_rejects_directory_as_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                cli.preflight_attachments([directory])


class ProfileTests(unittest.TestCase):
    def test_authenticated_profile_requires_explicit_boolean(self):
        self.assertTrue(
            cli.profile_authentication(
                {"smtp_auth": True},
                "test",
            )
        )

        self.assertFalse(
            cli.profile_authentication(
                {"smtp_auth": False},
                "test",
            )
        )

    def test_rejects_non_boolean_authentication_setting(self):
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as context,
        ):
            cli.profile_authentication(
                {"smtp_auth": "true"},
                "test",
            )

        self.assertEqual(context.exception.code, 2)

    def test_selects_single_profile_automatically(self):
        result = cli.automatic_profile_from_store(
            {
                "alerts": {
                    "smtp_auth": False,
                }
            },
            Path("/tmp/profiles.json"),
            "personal",
        )

        self.assertEqual(result[0], "alerts")
        self.assertEqual(
            result[2],
            "/tmp/profiles.json",
        )

    def test_requires_choice_when_multiple_profiles_exist(self):
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as context,
        ):
            cli.automatic_profile_from_store(
                {
                    "first": {"smtp_auth": False},
                    "second": {"smtp_auth": False},
                },
                Path("/tmp/profiles.json"),
                "personal",
            )

        self.assertEqual(context.exception.code, 2)

    def test_uses_absolute_xdg_configuration_path(self):
        with patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": "/tmp/postout-test-config",
            },
            clear=False,
        ):
            self.assertEqual(
                cli.default_profiles_path(),
                Path(
                    "/tmp/postout-test-config/"
                    "postout/profiles.json"
                ),
            )


class CliPresentationTests(unittest.TestCase):
    def test_welcome_screen_contains_setup_guidance(self):
        output = io.StringIO()

        with redirect_stdout(output):
            cli.print_welcome()

        rendered = output.getvalue()

        self.assertIn("POSTOUT", rendered)
        self.assertIn("Recommended start", rendered)
        self.assertIn("postout config", rendered)
        self.assertIn(
            "postout config --system",
            rendered,
        )
        self.assertIn("postout --help", rendered)

    def test_direct_smtp_detects_explicit_host(self):
        args = SimpleNamespace(
            smtp_host="smtp.example.com",
            smtp_user="",
            smtp_pass="",
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                cli.direct_smtp_requested(args)
            )

    def test_direct_smtp_not_selected_without_direct_settings(self):
        args = SimpleNamespace(
            smtp_host="",
            smtp_user="",
            smtp_pass="",
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                cli.direct_smtp_requested(args)
            )


if __name__ == "__main__":
    unittest.main()
