"""Credential vault tests — real openssl round-trips against a temp vault.

Everything runs through the CLI exactly as an operator would, with the
passphrase supplied via MI_VAULT_PASSPHRASE (the documented non-interactive
path) and values via MI_VAULT_VALUE. No secret ever appears in argv.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_PY = os.path.join(REPO, "tools", "vault.py")


@unittest.skipUnless(shutil.which("openssl"), "system openssl required")
class TestVault(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vault-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "vault.enc")

    def run_vault(self, *args, passphrase="correct-horse", value=None,
                  expect_rc=0):
        env = dict(os.environ, MI_VAULT_PATH=self.path,
                   MI_VAULT_PASSPHRASE=passphrase)
        if value is not None:
            env["MI_VAULT_VALUE"] = value
        p = subprocess.run([sys.executable, VAULT_PY, *args],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, expect_rc,
                         "argv=%r stdout=%r stderr=%r" % (args, p.stdout, p.stderr))
        return p

    def test_init_creates_0600_file(self):
        self.run_vault("init")
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_set_get_roundtrip(self):
        self.run_vault("init")
        self.run_vault("set", "demo.login", value="336582315")
        out = self.run_vault("get", "demo.login").stdout.strip()
        self.assertEqual(out, "336582315")

    def test_list_shows_names_never_values(self):
        self.run_vault("init")
        self.run_vault("set", "notify.token", value="SECRET-VALUE-XYZ")
        p = self.run_vault("list")
        self.assertIn("notify.token", p.stdout)
        self.assertNotIn("SECRET-VALUE-XYZ", p.stdout + p.stderr)

    def test_wrong_passphrase_fails_and_leaks_nothing(self):
        self.run_vault("init")
        self.run_vault("set", "live.api_key", value="TOP-SECRET-123")
        p = self.run_vault("get", "live.api_key", passphrase="wrong",
                           expect_rc=1)
        self.assertNotIn("TOP-SECRET-123", p.stdout + p.stderr)

    def test_export_env_namespacing(self):
        self.run_vault("init")
        self.run_vault("set", "demo.login", value="336582315")
        self.run_vault("set", "live.api_key", value="NOPE")
        p = self.run_vault("export-env", "demo")
        self.assertIn("export MI_DEMO_LOGIN=336582315", p.stdout)
        self.assertNotIn("NOPE", p.stdout, "live.* must not leak into demo export")

    def test_rm_removes(self):
        self.run_vault("init")
        self.run_vault("set", "demo.login", value="1")
        self.run_vault("rm", "demo.login")
        self.run_vault("get", "demo.login", expect_rc=1)

    def test_set_requires_existing_vault(self):
        """A typo'd path must not silently create a second vault."""
        self.run_vault("set", "demo.login", value="1", expect_rc=1)


if __name__ == "__main__":
    unittest.main()
