"""Verify direct Telegram and legacy relay delivery without network requests."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SPEC = importlib.util.spec_from_file_location('monitor', Path(__file__).resolve().parents[1] / 'monitor.py')
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

TOKEN = '123456:TEST_TOKEN'
DIRECT = {'TG_TOKEN': TOKEN, 'TG_CHAT_ID': '-100123456'}
RELAY = {**DIRECT, 'RELAY_URL': 'https://relay.test/send', 'RELAY_AUTH': 'test-relay-secret'}


class Response:
    def __init__(self, body=b'{"ok":true}', status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class NotificationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, DIRECT, clear=True)
        env.start()
        self.addCleanup(env.stop)
        http = patch.object(monitor, 'urlopen', return_value=Response())
        self.http = http.start()
        self.addCleanup(http.stop)

    def test_absent_empty_and_whitespace_relay_url_send_directly(self):
        for config in (DIRECT, {**DIRECT, 'RELAY_URL': ''}, {**DIRECT, 'RELAY_URL': '  '}):
            with self.subTest(config=config), patch.dict(os.environ, config, clear=True):
                monitor.send('Тест ✅')
                request = self.http.call_args.args[0]
                self.assertEqual(request.full_url, f'https://api.telegram.org/bot{TOKEN}/sendMessage')
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(json.loads(request.data), {'text': 'Тест ✅', 'chat_id': DIRECT['TG_CHAT_ID']})
                self.assertEqual(self.http.call_args.kwargs['timeout'], 12)
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertEqual(headers, {'content-type': 'application/json; charset=utf-8'})

    def test_unused_relay_auth_is_not_required_or_forwarded_to_telegram(self):
        with patch.dict(os.environ, {'RELAY_AUTH': 'REPLACE_WITH_UNUSED_SECRET'}):
            monitor.send('test')
        request = self.http.call_args.args[0]
        self.assertNotIn('REPLACE_WITH_UNUSED_SECRET', str(request.header_items()))
        self.assertNotIn('REPLACE_WITH_UNUSED_SECRET', request.data.decode())

    def test_existing_relay_protocol_and_timeout_are_preserved(self):
        with patch.dict(os.environ, {**RELAY, 'RELAY_TIMEOUT_SEC': '7.5', 'TG_TIMEOUT_SEC': '2'}, clear=True):
            self.http.return_value = Response(b'accepted')
            monitor.send('relay test')
        request = self.http.call_args.args[0]
        self.assertEqual(request.full_url, RELAY['RELAY_URL'])
        self.assertEqual(json.loads(request.data), {'text': 'relay test', 'chat_id': DIRECT['TG_CHAT_ID']})
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers['x-relay-auth'], RELAY['RELAY_AUTH'])
        self.assertEqual(headers['x-tg-token'], TOKEN)
        self.assertEqual(self.http.call_args.kwargs['timeout'], 7.5)

    def test_direct_timeout_is_independent_of_relay_timeout(self):
        with patch.dict(os.environ, {'TG_TIMEOUT_SEC': '4', 'RELAY_TIMEOUT_SEC': '9'}):
            monitor.send('test')
        self.assertEqual(self.http.call_args.kwargs['timeout'], 4)

    def test_telegram_requires_explicit_json_ok_true(self):
        for body in (b'', b'accepted', b'[]', b'null', b'{}', b'{"ok":false}',
                     b'{"ok":1}', b'{"ok":true,"success":false}'):
            with self.subTest(body=body):
                self.http.return_value = Response(body)
                with self.assertRaises(RuntimeError):
                    monitor.send('test')

    def test_long_unicode_message_response_is_not_truncated_at_8kb(self):
        message = 'я' * 4000
        body = json.dumps({'ok': True, 'result': {'text': message}}).encode()
        self.assertGreater(len(body), 8192)
        self.http.return_value = Response(body)
        monitor.send(message)

    def test_relay_application_errors_are_rejected(self):
        with patch.dict(os.environ, RELAY, clear=True):
            for body in (b'{"ok":false}', b'{"success":false}', b'{invalid json'):
                with self.subTest(body=body):
                    self.http.return_value = Response(body)
                    with self.assertRaises(RuntimeError):
                        monitor.send('test')

    def test_non_success_status_is_rejected_for_both_transports(self):
        for config in (DIRECT, RELAY):
            with self.subTest(config=config), patch.dict(os.environ, config, clear=True):
                self.http.return_value = Response(status=503)
                with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
                    monitor.send('test')

    def test_failure_of_configured_relay_does_not_switch_to_direct_delivery(self):
        with patch.dict(os.environ, RELAY, clear=True):
            self.http.side_effect = URLError('relay down')
            with self.assertRaises(RuntimeError):
                monitor.send('test')
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args[0].full_url, RELAY['RELAY_URL'])

    def test_error_tracebacks_do_not_expose_tokens_urls_or_response_bodies(self):
        url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
        errors = (
            HTTPError(url, 401, f'echo {TOKEN}', None, None),
            URLError(f'failed {url}'),
            ValueError(f'invalid URL {url}'),
            OSError(f'network error {TOKEN}'),
            monitor.HTTPException(f'HTTP error {TOKEN}'),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.http.side_effect = error
                try:
                    monitor.send('test')
                except RuntimeError:
                    rendered = traceback.format_exc()
                else:
                    self.fail('Expected delivery failure')
                self.assertNotIn(TOKEN, rendered)
                self.assertNotIn(url, rendered)
        self.http.side_effect = None
        self.http.return_value = Response(json.dumps({'ok': False, 'description': TOKEN}).encode())
        with self.assertRaises(RuntimeError) as caught:
            monitor.send('test')
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_required_config_depends_on_selected_transport(self):
        for config, missing in (
            ({'TG_CHAT_ID': '123'}, 'TG_TOKEN'),
            ({'TG_TOKEN': TOKEN}, 'TG_CHAT_ID'),
            ({**DIRECT, 'RELAY_URL': RELAY['RELAY_URL']}, 'RELAY_AUTH'),
            ({**DIRECT, 'TG_TOKEN': 'REPLACE_WITH_TOKEN'}, 'TG_TOKEN'),
        ):
            with self.subTest(missing=missing), patch.dict(os.environ, config, clear=True):
                with self.assertRaisesRegex(RuntimeError, missing):
                    monitor.send('test')
        self.http.assert_not_called()

    def test_invalid_relay_url_is_rejected_without_echoing_it(self):
        for url in ('=https://relay.test', 'not-a-url', 'file:///tmp/relay',
                    'https://[invalid', 'https://relay.example.com/webhook'):
            with self.subTest(url=url), patch.dict(os.environ, {**RELAY, 'RELAY_URL': url}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'RELAY_URL') as caught:
                    monitor.send('test')
                self.assertNotIn(url, str(caught.exception))
        self.http.assert_not_called()

    def test_failed_startup_is_retried_and_committed_only_after_success(self):
        for config in (DIRECT, RELAY):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, config, clear=True):
                base = Path(directory)
                (base / '.env').write_text('')
                self.http.side_effect = None
                self.http.reset_mock()
                self.http.return_value = Response(b'{"ok":false}')
                with patch.object(monitor, 'BASE', base), \
                        patch.object(monitor, 'check_metrics', return_value={}):
                    with self.assertLogs(level='ERROR'), self.assertRaises(RuntimeError):
                        monitor.main()
                    saved = json.loads((base / 'state.json').read_text())
                    self.assertIn('boot_id', saved)
                    self.assertFalse(saved.get('first_run_sent', False))
                    self.http.return_value = Response()
                    monitor.main()
                    saved = json.loads((base / 'state.json').read_text())
                    self.assertTrue(saved['first_run_sent'])
                    self.assertEqual(self.http.call_count, 2)
                    monitor.main()
                    self.assertEqual(self.http.call_count, 2)


if __name__ == '__main__':
    unittest.main()
