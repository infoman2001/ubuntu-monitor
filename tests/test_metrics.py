"""Interval CPU accounting, load normalization and persisted alert behavior."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('monitor', Path(__file__).resolve().parents[1] / 'monitor.py')
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def sample(user=0, idle=0, iowait=0, stamp=100, boot='boot', cpus=2):
    return {'boot_id': boot, 'monotonic': stamp, 'cpu_count': cpus,
            'counters': {'user': user, 'nice': 0, 'system': 0, 'idle': idle,
                         'iowait': iowait, 'irq': 0, 'softirq': 0, 'steal': 0}}


class CpuAverageTests(unittest.TestCase):
    def test_busy_minute_followed_by_four_idle_minutes_is_twenty_percent(self):
        # Two CPUs: 600 CPU-seconds in a 300-second wall-clock interval.
        average = monitor.cpu_average(sample(), sample(user=120, idle=480, stamp=400))
        self.assertEqual(average, (20.0, 300))

    def test_all_idle_and_all_busy(self):
        self.assertEqual(monitor.cpu_average(sample(), sample(idle=600, stamp=400))[0], 0)
        self.assertEqual(monitor.cpu_average(sample(), sample(user=600, stamp=400))[0], 100)

    def test_iowait_is_not_cpu_execution(self):
        self.assertEqual(monitor.cpu_average(sample(), sample(user=120, idle=300, iowait=180, stamp=400))[0], 20)

    def test_decreasing_iowait_is_clamped(self):
        self.assertEqual(monitor.cpu_average(sample(iowait=30), sample(user=120, idle=480, iowait=20, stamp=400))[0], 20)

    def test_guest_time_not_counted_twice(self):
        before = SimpleNamespace(_asdict=lambda: {'user': 0, 'nice': 0, 'idle': 0, 'guest': 0, 'guest_nice': 0})
        after = SimpleNamespace(_asdict=lambda: {'user': 120, 'nice': 0, 'idle': 480, 'guest': 120, 'guest_nice': 0})
        with patch.object(monitor.psutil, 'cpu_times', side_effect=[before, after]), \
                patch.object(monitor.psutil, 'cpu_count', return_value=2), \
                patch.object(monitor.time, 'monotonic', side_effect=[100, 400]):
            first = monitor.cpu_snapshot('boot')
            second = monitor.cpu_snapshot('boot')
        self.assertNotIn('guest', second['counters'])
        self.assertEqual(monitor.cpu_average(first, second), (20.0, 300))

    def test_missing_reboot_changed_cpu_count_and_counter_regression(self):
        for previous, current in (
            (None, sample()),
            (sample(), sample(boot='new-boot', stamp=400)),
            (sample(), sample(cpus=4, stamp=400)),
            (sample(user=100), sample(user=50, idle=600, stamp=400)),
        ):
            with self.subTest(previous=previous, current=current):
                self.assertIsNone(monitor.cpu_average(previous, current))

    def test_invalid_intervals_and_malformed_snapshots_are_skipped(self):
        for previous, current in (
            (sample(), sample(stamp=100.5)),
            (sample(), sample(stamp=90)),
            (sample(), sample(stamp=400)),  # no counter progress
            ({}, sample()),
            ({**sample(), 'counters': None}, sample(stamp=400)),
            ({**sample(), 'monotonic': 'bad'}, sample(stamp=400)),
            (sample(), sample(user=float('nan'), stamp=400)),
        ):
            with self.subTest(previous=previous, current=current):
                self.assertIsNone(monitor.cpu_average(previous, current))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DISK_PATHS': '', 'CPU_MAX_PERCENT': '90'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for name, value in (
            ('virtual_memory', SimpleNamespace(percent=25, available=1024**3)),
            ('swap_memory', SimpleNamespace(total=0)),
        ):
            mock = patch.object(monitor.psutil, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        load = patch.object(monitor.os, 'getloadavg', return_value=(0.1, 0.2, 0.3))
        self.load = load.start()
        self.addCleanup(load.stop)

    def check(self, state, snapshot):
        with patch.object(monitor, 'cpu_snapshot', return_value=snapshot):
            return monitor.check_metrics(state, snapshot['boot_id'])

    def test_baseline_skips_cpu_but_still_checks_other_metrics(self):
        state = {'alerts': {'cpu': {'streak': 2, 'notified': True, 'last_sent': 123}}}
        metrics = self.check(state, sample())
        self.assertNotIn('cpu', metrics)
        self.assertIn('ram', metrics)
        self.assertIn('load5', metrics)
        self.assertEqual(state['cpu_sample'], sample())
        self.assertEqual(state['alerts']['cpu'], {'streak': 0, 'notified': True, 'last_sent': 123})

    def test_cpu_threshold_uses_average_and_actual_elapsed_interval(self):
        state = {'cpu_sample': sample()}
        metrics = self.check(state, sample(user=570, idle=30, stamp=700))
        bad, text, required = metrics['cpu']
        self.assertTrue(bad)
        self.assertIn('95.0%', text)
        self.assertIn('10.0 мин', text)
        self.assertEqual(required, 3)

    def test_load_is_normalized_by_logical_cpu_count(self):
        self.load.return_value = (8, 4.2, 2)
        for cpus, expected in ((2, True), (8, False)):
            with self.subTest(cpus=cpus):
                bad, text, checks = self.check({}, sample(cpus=cpus))['load5']
                self.assertEqual(bad, expected)
                self.assertIn('4.20', text)
                self.assertEqual(checks, 2)

    def test_load_threshold_can_be_configured_or_disabled(self):
        self.load.return_value = (8, 3, 2)
        with patch.dict(os.environ, {'LOAD5_MAX_PER_CPU': '2', 'LOAD_CONSECUTIVE_CHECKS': '3'}):
            bad, _, checks = self.check({}, sample())['load5']
            self.assertFalse(bad)
            self.assertEqual(checks, 3)
        with patch.dict(os.environ, {'CHECK_LOAD_AVERAGE': '0'}):
            self.assertNotIn('load5', self.check({}, sample()))

    def test_cpu_baseline_survives_failed_notification_and_next_run_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.env').write_text('RELAY_URL=https://relay.test\nRELAY_AUTH=test\nTG_TOKEN=test\nTG_CHAT_ID=123\n')
            boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            old_alert = {'streak': 3, 'notified': True, 'last_sent': 123}
            (base / 'state.json').write_text(json.dumps({'boot_id': boot_id, 'first_run_sent': True,
                                                       'alerts': {'cpu': old_alert}}))
            with patch.object(monitor, 'BASE', base), \
                    patch.object(monitor, 'send') as send:
                with patch.object(monitor, 'cpu_snapshot', return_value=sample(boot=boot_id)):
                    monitor.main()
                # No false recovery while waiting for the first full interval.
                send.assert_not_called()
                saved = json.loads((base / 'state.json').read_text())
                self.assertTrue(saved['alerts']['cpu']['notified'])
                send.side_effect = OSError('simulated relay failure')
                second = sample(user=120, idle=480, stamp=400, boot=boot_id)
                with patch.object(monitor, 'cpu_snapshot', return_value=second):
                    with self.assertLogs(level='ERROR'), self.assertRaises(RuntimeError):
                        monitor.main()
                saved = json.loads((base / 'state.json').read_text())
                self.assertEqual(saved['cpu_sample'], second)
                self.assertTrue(saved['alerts']['cpu']['notified'])
                send.side_effect = None
                third = sample(user=240, idle=960, stamp=700, boot=boot_id)
                with patch.object(monitor, 'cpu_snapshot', return_value=third):
                    monitor.main()
                self.assertIn('20.0%', send.call_args.args[0])
                saved = json.loads((base / 'state.json').read_text())
                self.assertFalse(saved['alerts']['cpu']['notified'])

    def test_reboot_preserves_announced_cpu_alert_until_new_interval(self):
        state = {'cpu_sample': sample(), 'alerts': {'cpu': {'streak': 2, 'notified': True}}}
        metrics = self.check(state, sample(boot='new-boot', stamp=20))
        self.assertNotIn('cpu', metrics)
        self.assertTrue(state['alerts']['cpu']['notified'])
        metrics = self.check(state, sample(user=60, idle=540, boot='new-boot', stamp=320))
        self.assertFalse(metrics['cpu'][0])


if __name__ == '__main__':
    unittest.main()
