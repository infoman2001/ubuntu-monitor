"""Verify updates against a local bare Git remote, without network or sudo."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.publisher = self.root / 'publisher'
        self.checkout = self.root / 'checkout'
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('clone', str(self.remote), str(self.publisher))
        self.git('config', 'user.name', 'Test', cwd=self.publisher)
        self.git('config', 'user.email', 'test@example.com', cwd=self.publisher)
        (self.publisher / 'scripts').mkdir()
        script = (PROJECT / 'scripts/update.sh').read_text().replace('[[ $EUID -ne 0 ]]', 'true')
        (self.publisher / 'scripts/update.sh').write_text(script)
        (self.publisher / 'scripts/install.sh').write_text('# install stub\n')
        self.commit_publish('initial')
        self.git('clone', str(self.remote), str(self.checkout))
        self.git('config', 'user.name', 'Test', cwd=self.checkout)
        self.git('config', 'user.email', 'test@example.com', cwd=self.checkout)
        bindir = self.root / 'bin'
        bindir.mkdir()
        sudo = bindir / 'sudo'
        sudo.write_text('#!/bin/bash\ntouch "$DEPLOY_MARKER"\nexit "${FAIL_INSTALL:-0}"\n')
        sudo.chmod(0o755)
        self.marker = self.root / 'deployed'
        self.env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}', DEPLOY_MARKER=str(self.marker))

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=cwd or self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit_publish(self, message):
        self.git('add', '.', cwd=self.publisher)
        self.git('commit', '-m', message, cwd=self.publisher)
        self.git('push', 'origin', 'main', cwd=self.publisher)

    def update(self, **env):
        return subprocess.run(['bash', 'scripts/update.sh'], cwd=self.checkout,
                              env={**self.env, **env}, capture_output=True, text=True)

    def test_fast_forward_and_deploy(self):
        (self.publisher / 'new-version').write_text('v2')
        self.commit_publish('v2')
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.checkout / 'new-version').read_text(), 'v2')
        self.assertTrue(self.marker.exists())

    def test_local_changes_are_preserved_and_rejected(self):
        (self.checkout / 'scripts/install.sh').write_text('# local change\n')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local change', (self.checkout / 'scripts/install.sh').read_text())
        self.assertFalse(self.marker.exists())

    def test_untracked_files_are_preserved_and_rejected(self):
        (self.checkout / 'notes').write_text('keep')
        self.assertNotEqual(self.update().returncode, 0)
        self.assertEqual((self.checkout / 'notes').read_text(), 'keep')
        self.assertFalse(self.marker.exists())

    def test_unpublished_local_commit_is_rejected(self):
        (self.checkout / 'notes').write_text('keep')
        self.git('add', '.', cwd=self.checkout)
        self.git('commit', '-m', 'local only', cwd=self.checkout)
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_failed_install_can_be_retried_without_new_commit(self):
        self.assertNotEqual(self.update(FAIL_INSTALL='1').returncode, 0)
        self.marker.unlink()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
