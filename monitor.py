#!/usr/bin/env python3
"""Lightweight host health monitor, executed periodically by systemd."""
import fcntl
import json
import logging
import math
import os
import socket
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

BASE = Path(os.environ.get("UBUNTU_MONITOR_HOME", Path(__file__).resolve().parent))


def load_env(path):
    if not path.exists():
        raise RuntimeError(f"Missing config: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, sep, val = line.partition('=')
        if not sep:
            continue
        val = val.strip()
        if val and val[0] in ("'", '"') and val[-1] == val[0]:
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


def cfg_float(key, default):
    return float(os.getenv(key, str(default)))


def cfg_int(key, default):
    return int(os.getenv(key, str(default)))


def read_state(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.monitor-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def send(text):
    url = os.environ['RELAY_URL']
    auth = os.environ['RELAY_AUTH']
    token = os.environ['TG_TOKEN']
    chat_id = os.environ['TG_CHAT_ID']
    body = json.dumps({'text': text, 'chat_id': chat_id}, ensure_ascii=False).encode('utf-8')
    request = Request(url, data=body, headers={
        'Content-Type': 'application/json; charset=utf-8',
        'X-Relay-Auth': auth,
        'X-TG-Token': token,
    }, method='POST')
    with urlopen(request, timeout=cfg_float('RELAY_TIMEOUT_SEC', 12)) as response:
        reply = response.read(8192).decode('utf-8', errors='replace')
        if not 200 <= response.status < 300:
            raise RuntimeError(f'Relay returned HTTP {response.status}')
        # A HTTP 200 might still contain an application-level failure.
        if reply.strip().startswith('{'):
            parsed = json.loads(reply)
            if parsed.get('ok') is False or parsed.get('success') is False:
                raise RuntimeError('Relay reported delivery failure')


def cpu_snapshot(boot_id):
    # On Linux psutil reads /proc/stat. guest times are already included in
    # user/nice; counting them again would distort the utilization percentage.
    counters = {key: value for key, value in psutil.cpu_times()._asdict().items()
                if key not in ('guest', 'guest_nice')}
    return {'boot_id': boot_id, 'monotonic': time.monotonic(),
            'cpu_count': psutil.cpu_count() or 1, 'counters': counters}


def cpu_average(previous, current):
    """Return (busy percent, elapsed seconds), or None without a valid pair."""
    if not isinstance(previous, dict):
        return None
    try:
        if (previous['boot_id'] != current['boot_id'] or
                previous['cpu_count'] != current['cpu_count']):
            return None
        elapsed = current['monotonic'] - previous['monotonic']
        if not math.isfinite(elapsed) or elapsed < 1:
            return None
        before, after = previous['counters'], current['counters']
        if before.keys() != after.keys():
            return None
        deltas = {}
        for key, value in after.items():
            delta = value - before[key]
            if not math.isfinite(delta):
                return None
            # Linux iowait may decrease; clamp its delta as psutil does.
            # Other regressions invalidate the baseline (reset/hotplug).
            if delta < 0 and key != 'iowait':
                return None
            deltas[key] = max(0.0, delta)
        total = sum(deltas.values())
        if total <= 0:
            return None
        busy = total - deltas['idle'] - deltas.get('iowait', 0)
        return max(0.0, min(100.0, busy / total * 100)), elapsed
    except (KeyError, TypeError, AttributeError):
        # A malformed or older saved snapshot should not stop RAM/disk checks.
        return None


def check_metrics(state, boot_id):
    metrics = {}
    snapshot = cpu_snapshot(boot_id)
    average = cpu_average(state.get('cpu_sample'), snapshot)
    state['cpu_sample'] = snapshot
    if average is None:
        # Skip CPU only. Preserve an announced alert until a real interval can
        # confirm recovery, and don't mix old instantaneous streaks with averages.
        record = state.get('alerts', {}).get('cpu')
        if record is not None:
            record['streak'] = 0
        logging.info('CPU average pending: baseline saved; waiting for next check')
    else:
        cpu, elapsed = average
        interval = f'{elapsed / 60:.1f} мин' if elapsed >= 60 else f'{elapsed:.1f} с'
        metrics['cpu'] = (cpu >= cfg_float('CPU_MAX_PERCENT', 90),
                          f'CPU: {cpu:.1f}% в среднем за {interval} '
                          f'(порог {cfg_float("CPU_MAX_PERCENT", 90):g}%)',
                          cfg_int('CPU_CONSECUTIVE_CHECKS', 3))
        logging.info('CPU average: %.1f%% over %.1f seconds', cpu, elapsed)
    if cfg_int('CHECK_LOAD_AVERAGE', 1):
        load5 = os.getloadavg()[1]
        cpus = snapshot['cpu_count']
        ratio = load5 / cpus
        limit = cfg_float('LOAD5_MAX_PER_CPU', 1.0)
        metrics['load5'] = (ratio >= limit,
                            f'Load average (5 мин): {load5:.2f}, CPU: {cpus}; '
                            f'{ratio:.2f} на CPU (порог {limit:g})',
                            cfg_int('LOAD_CONSECUTIVE_CHECKS', 2))
        logging.info('Load average (5 min): %.2f, %.2f per CPU (%d CPUs)',
                     load5, ratio, cpus)
    mem = psutil.virtual_memory()
    metrics['ram'] = (mem.percent >= cfg_float('RAM_MAX_PERCENT', 90),
                      f'RAM: {mem.percent:.1f}% (свободно {mem.available / 1024**3:.1f} GB)',
                      cfg_int('RAM_CONSECUTIVE_CHECKS', 2))
    swap = psutil.swap_memory()
    if swap.total:
        metrics['swap'] = (swap.percent >= cfg_float('SWAP_MAX_PERCENT', 80),
                           f'Swap: {swap.percent:.1f}%',
                           cfg_int('SWAP_CONSECUTIVE_CHECKS', 2))
    paths = [p.strip() for p in os.getenv('DISK_PATHS', '/').split(',') if p.strip()]
    for path in dict.fromkeys(paths):
        disk = psutil.disk_usage(path)
        free_gb = disk.free / (1024 ** 3)
        free_percent = disk.free / disk.total * 100 if disk.total else 0
        min_gb = cfg_float('DISK_MIN_FREE_GB', 5)
        min_percent = cfg_float('DISK_MIN_FREE_PERCENT', 10)
        # Either threshold can be disabled by setting it to 0.
        bad = (min_gb > 0 and free_gb < min_gb) or (min_percent > 0 and free_percent < min_percent)
        metrics[f'disk:{path}'] = (bad,
            f'Диск {path}: свободно {free_gb:.1f} GB ({free_percent:.1f}%)', 1)
        if cfg_int('CHECK_INODES', 1):
            vfs = os.statvfs(path)
            if vfs.f_files:
                percent = vfs.f_favail / vfs.f_files * 100
                metrics[f'inodes:{path}'] = (
                    percent < cfg_float('INODES_MIN_FREE_PERCENT', 5),
                    f'Inodes {path}: свободно {percent:.1f}%', 1)
    return metrics


def main():
    load_env(BASE / '.env')
    for key in ('RELAY_URL', 'RELAY_AUTH', 'TG_TOKEN', 'TG_CHAT_ID'):
        if not os.getenv(key):
            raise RuntimeError(f'Missing required environment variable: {key}')
    state_path = BASE / 'state.json'
    lock_path = BASE / 'monitor.lock'
    with open(lock_path, 'a+', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info('Another monitor process is running')
            return
        state = read_state(state_path)
        now = time.time()
        host = os.getenv('SERVER_NAME') or socket.gethostname()
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        first_run = 'boot_id' not in state
        rebooted = not first_run and state['boot_id'] != boot_id
        # Maintain a pending event until the relay acknowledges the POST.
        if rebooted:
            state['pending_boot'] = boot_id
            # Clear accumulated critical streak after a reboot; retain announced problems
            # so that recovery is still reported when checks turn green.
            for val in state.get('alerts', {}).values():
                val['streak'] = 0
        state['boot_id'] = boot_id
        metrics = check_metrics(state, boot_id)
        alerts = state.setdefault('alerts', {})
        new_problems, recoveries, reminders = [], [], []
        remind_secs = cfg_int('REMIND_EVERY_HOURS', 12) * 3600
        for key, (bad, description, required) in metrics.items():
            record = alerts.setdefault(key, {'streak': 0, 'notified': False, 'last_sent': 0})
            if bad:
                record['streak'] = int(record.get('streak', 0)) + 1
                if record['streak'] >= required:
                    if not record.get('notified'):
                        new_problems.append((key, description))
                    elif remind_secs > 0 and now - record.get('last_sent', now) >= remind_secs:
                        reminders.append((key, description))
            else:
                record['streak'] = 0
                if record.get('notified'):
                    recoveries.append((key, description))
        # Prepare outgoing messages; on send failure state flags are NOT committed.
        outgoing = []
        if state.get('pending_boot') == boot_id and (rebooted or not first_run):
            started = datetime.fromtimestamp(psutil.boot_time()).strftime('%d.%m.%Y %H:%M:%S')
            outgoing.append(('boot', '🔄 Сервер перезагрузился\n'
                f'🖥 {host}\n🕒 Запущен: {started}\n'
                f'⏱ Uptime: {int(now - psutil.boot_time()) // 60} мин'))
        if first_run and cfg_int('NOTIFY_ON_FIRST_RUN', 1) and not state.get('first_run_sent'):
            outgoing.append(('startup', f'✅ Мониторинг запущен\n🖥 {host}'))
        if new_problems:
            outgoing.append(('new', '🚨 Проблемы на сервере\n🖥 ' + host + '\n\n' +
                             '\n'.join('🔴 ' + item[1] for item in new_problems)))
        if recoveries:
            outgoing.append(('recovery', '✅ Показатели восстановились\n🖥 ' + host + '\n\n' +
                             '\n'.join('🟢 ' + item[1] for item in recoveries)))
        if reminders:
            outgoing.append(('reminder', '⚠️ Проблемы сохраняются\n🖥 ' + host + '\n\n' +
                             '\n'.join('🔴 ' + item[1] for item in reminders)))
        write_state(state_path, state)
        failed = False
        for kind, msg in outgoing:
            try:
                send(msg)
            except (OSError, ValueError, HTTPError, URLError, RuntimeError) as exc:
                failed = True
                logging.error('Failed to send %s: %s', kind, exc)
                continue
            if kind == 'boot':
                state.pop('pending_boot', None)
            elif kind == 'startup':
                state['first_run_sent'] = True
            else:
                changes = {'new': (new_problems, True),
                           'recovery': (recoveries, False),
                           'reminder': (reminders, True)}
                items, value = changes[kind]
                for key, _ in items:
                    alerts[key]['notified'] = value
                    alerts[key]['last_sent'] = now
            write_state(state_path, state)
            logging.info('Sent %s notification', kind)
        if failed:
            raise RuntimeError('Some relay notifications failed; will retry next run')
        logging.info('Checked %d metrics; %d notifications', len(metrics), len(outgoing))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        main()
    except Exception:
        logging.exception('Monitor check failed')
        raise
