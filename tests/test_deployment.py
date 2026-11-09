"""Exercise deployment failures using disposable paths and fake OS services."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        shutil.copytree(PROJECT, self.source, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.target = self.root / 'deployment'
        self.units = self.root / 'units'
        self.units.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', TEST_ROOT=str(self.root))
        self.env['PYTHONPATH'] = str(self.bin)
        (self.bin / 'psutil.py').write_text('def virtual_memory(): pass\ndef disk_usage(path): pass\n')
        self.script = self.source / 'scripts/install.sh'
        script = self.script.read_text()
        script = script.replace('TARGET=/opt/ubuntu-monitor', f'TARGET={self.target}')
        script = script.replace('UNITS=/etc/systemd/system', f'UNITS={self.units}')
        script = script.replace('[[ $EUID -eq 0 ]]', 'true')
        script = script.replace('/run/lock/ubuntu-monitor-install.lock', str(self.root / 'install.lock'))
        script = script.replace('/var/backups/ubuntu-monitor/', str(self.root / 'backups') + '/')
        self.script.write_text(script)
        self.command('systemctl', '''#!/bin/bash
printf '%s\n' "$*" >> "$TEST_ROOT/systemctl.log"
case "$1" in
show)
    if [[ -f $TEST_ROOT/units/$2 ]]; then echo loaded; else echo not-found; fi
    ;;
is-active|is-enabled)
    unit=${@: -1}
    [[ -f $TEST_ROOT/units/$unit ]] || exit 3
    ;;
stop)
    shift
    for unit in "$@"; do
        if [[ ! -f $TEST_ROOT/units/$unit ]]; then
            echo "Unit $unit not loaded" >&2
            exit 5
        fi
        [[ ${FAIL_STOP_UNIT:-} != "$unit" ]] || exit 1
    done
    ;;
enable|disable)
    unit=${@: -1}
    [[ -f $TEST_ROOT/units/$unit ]] || { echo "Unit $unit does not exist" >&2; exit 5; }
    if [[ $1 == enable && ${FAIL_ENABLE:-0} == 1 && $* == *--now* ]]; then exit 1; fi
    ;;
esac
''')
        self.command('systemd-analyze', '#!/bin/bash\nexit "${FAIL_VERIFY:-0}"\n')
        # Keep real Python for parsing and compilation; replace only venv/pip.
        self.command('python3', f'''#!/bin/bash
if [[ $1 == -m && $2 == venv ]]; then
    mkdir -p "$3/bin"
    cp "$TEST_ROOT/bin/venv-python" "$3/bin/python"
    exit 0
fi
exec {sys.executable} "$@"
''')
        self.command('venv-python', f'''#!/bin/bash
if [[ $1 == -m && $2 == pip ]]; then exit "${{FAIL_PIP:-0}}"; fi
exec {sys.executable} "$@"
''')

    def command(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_install(self, **env):
        result = subprocess.run(['bash', str(self.script)], env={**self.env, **env}, capture_output=True, text=True)
        return result

    def configure_existing(self):
        self.target.mkdir()
        (self.target / '.env').write_text('RELAY_URL=https://relay.test/send\nRELAY_AUTH=secret\nTG_TOKEN=secret\nTG_CHAT_ID=123\n')
        (self.target / 'state.json').write_text('{"boot_id":"preserve-me"}')
        (self.target / 'monitor.py').write_text('# legacy running code\n')
        for name in ('ubuntu-monitor.service', 'ubuntu-monitor.timer'):
            (self.units / name).write_text('legacy unit: ' + name)

    def assert_preserved(self):
        self.assertEqual((self.target / 'state.json').read_text(), '{"boot_id":"preserve-me"}')
        self.assertIn('TG_TOKEN=secret', (self.target / '.env').read_text())
        self.assertEqual((self.target / 'monitor.py').read_text(), '# legacy running code\n')

    def test_first_install_creates_private_template_without_starting(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / '.env').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / 'systemctl.log').exists())
        self.assertFalse((self.target / 'current').exists())

    def test_placeholder_configuration_is_rejected(self):
        self.run_install()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'current').exists())

    def test_fresh_install_with_config_and_no_systemd_units(self):
        self.configure_existing()
        for unit in self.units.iterdir():
            unit.unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.target / 'current/venv/bin/python').exists())
        self.assertTrue((self.units / 'ubuntu-monitor.timer').exists())
        log = (self.root / 'systemctl.log').read_text()
        self.assertNotIn('stop ubuntu-monitor', log)
        self.assertIn('enable --now ubuntu-monitor.timer', log)
        self.assert_preserved()

    def test_failed_fresh_install_does_not_stop_or_disable_missing_units(self):
        self.configure_existing()
        for unit in self.units.iterdir():
            unit.unlink()
        result = self.run_install(FAIL_VERIFY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('restoring previous release', result.stderr)
        self.assertNotIn('not loaded', result.stderr)
        self.assertNotIn('does not exist', result.stderr)
        self.assertFalse((self.target / 'current').exists())
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertEqual(list((self.target / 'releases').iterdir()), [])
        self.assert_preserved()

    def test_failure_to_stop_existing_unit_still_aborts_update(self):
        self.configure_existing()
        result = self.run_install(FAIL_STOP_UNIT='ubuntu-monitor.timer')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'current').exists())
        self.assertIn('legacy unit', (self.units / 'ubuntu-monitor.service').read_text())
        self.assert_preserved()

    def test_migration_and_second_deployment_preserve_config_and_state(self):
        self.configure_existing()
        for _ in range(2):
            previous = (self.target / 'current').resolve() if (self.target / 'current').exists() else None
            result = self.run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((self.target / 'current/venv/bin/python').exists())
            self.assertNotEqual((self.target / 'current').resolve(), previous)
            self.assert_preserved()

    def test_dependency_failure_does_not_stop_running_monitor(self):
        self.configure_existing()
        result = self.run_install(FAIL_PIP='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('stop ', (self.root / 'systemctl.log').read_text())
        self.assertFalse((self.target / 'current').exists())
        self.assert_preserved()

    def test_failed_unit_validation_restores_legacy_units(self):
        self.configure_existing()
        result = self.run_install(FAIL_VERIFY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'current').exists())
        self.assertIn('legacy unit', (self.units / 'ubuntu-monitor.service').read_text())
        self.assertIn('start ubuntu-monitor.timer', (self.root / 'systemctl.log').read_text())
        self.assert_preserved()

    def test_failed_timer_start_restores_previous_release(self):
        self.configure_existing()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        previous = (self.target / 'current').resolve()
        result = self.run_install(FAIL_ENABLE='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / 'current').resolve(), previous)
        self.assertTrue((previous / 'venv/bin/python').exists())
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
