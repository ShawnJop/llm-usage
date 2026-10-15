from pathlib import Path
import sqlite3
import tempfile
import unittest

from diagnose_desktop import read_claude_cookie_rows, redact


class RedactTests(unittest.TestCase):
    def test_masks_username_in_home_and_users_paths(self):
        home = Path(r"C:\Users\alice")

        text = r"APPDATA=C:\Users\alice\AppData\Roaming; db=c:\users\ALICE\x\Cookies"

        self.assertEqual(
            redact(text, home=home, username="alice"),
            r"APPDATA=C:\Users\<user>\AppData\Roaming; db=c:\users\<user>\x\Cookies",
        )

    def test_masks_profile_outside_users_directory(self):
        home = Path(r"D:\Profiles\bob")

        self.assertEqual(
            redact(r"D:\Profiles\bob\.claude", home=home, username="bob"),
            r"D:\Profiles\<user>\.claude",
        )

    def test_masks_username_in_repr_escaped_paths(self):
        home = Path(r"C:\Users\alice")

        text = str([r"C:\Users\alice\AppData\Roaming\Claude\Network\Cookies"])

        self.assertEqual(
            redact(text, home=home, username="alice"),
            str([r"C:\Users\<user>\AppData\Roaming\Claude\Network\Cookies"]),
        )

    def test_keeps_names_that_only_contain_username(self):
        home = Path(r"C:\Users\al")

        self.assertEqual(
            redact(r"C:\Users\al\AppData\Local\alpha", home=home, username="al"),
            r"C:\Users\<user>\AppData\Local\alpha",
        )


class ClaudeCookieRowsTests(unittest.TestCase):
    def test_reads_only_claude_and_anthropic_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "Cookies"
            conn = sqlite3.connect(database)
            try:
                conn.execute(
                    "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
                )
                conn.executemany(
                    "INSERT INTO cookies VALUES (?, ?, ?, ?)",
                    [
                        (".claude.ai", "sessionKey", b"abc", 3),
                        ("claude.ai", "lastActiveOrg", b"de", 2),
                        ("api.anthropic.com", "cf", b"f", 1),
                        (".google.com", "NID", b"secret", 9),
                        ("evilclaude.ai", "x", b"y", 8),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            rows = read_claude_cookie_rows(database)

        self.assertEqual(
            rows,
            [
                (".claude.ai", "sessionKey", 3, 3),
                ("claude.ai", "lastActiveOrg", 2, 2),
                ("api.anthropic.com", "cf", 1, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
