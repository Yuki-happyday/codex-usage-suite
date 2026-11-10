import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_usage_watch as watch


def event(kind, **payload):
    return (json.dumps({
        'type': 'event_msg',
        'payload': {'type': kind, **payload},
    }, ensure_ascii=False) + '\n').encode('utf-8')


def count(tokens):
    return event('token_count', info={'last_token_usage': {'total_tokens': tokens}},
                 test_text='测试')


START = event('task_started')
END = event('task_complete')


class LogPollingTests(unittest.TestCase):
    def run_polls(self, initial, batches, saved_offsets=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / 'state.json'
            for name, data in initial.items():
                (root / name).write_bytes(data)
            if saved_offsets is not None:
                state_file.write_text(json.dumps({
                    'offsets': {str(root / name): value for name, value in saved_offsets.items()},
                }), encoding='utf-8')
            batches = iter(batches)
            totals = []
            offsets = []

            def sleep(_):
                state = json.loads(state_file.read_text(encoding='utf-8'))
                offsets.append({Path(p).name: v for p, v in state['offsets'].items()})
                batch = next(batches, None)
                if batch is None:
                    raise KeyboardInterrupt
                for name, data in batch.items():
                    with (root / name).open('ab') as log:
                        log.write(data)

            def notification(turn, state):
                totals.append(turn.total)
                return '', [], ''

            with patch.multiple(watch, BASE=str(root), STATE_FILE=str(state_file),
                                LOG_FILE=str(root / 'watch.log'),
                                scan_files=lambda: sorted(map(str, root.glob('*.jsonl'))),
                                build_notification=notification, notify=lambda *_: True), \
                    patch.object(watch.threading, 'Thread'), \
                    patch.object(ctypes, 'windll', create=True), \
                    patch.object(watch.time, 'sleep', side_effect=sleep):
                watch.main()
            return totals, offsets

    def test_interleaved_sessions_settle_separately(self):
        a, b = 'a.jsonl', 'b.jsonl'
        totals, _ = self.run_polls({a: b'', b: b''}, [
            {a: START + count(111)}, {b: START + count(222)}, {a: END}, {b: END},
        ])
        self.assertEqual(totals, [111, 222])

    def test_new_file_is_read_from_start(self):
        totals, _ = self.run_polls({}, [{'a.jsonl': START + count(111) + END}])
        self.assertEqual(totals, [111])

    def test_offset_waits_for_newline_and_keeps_utf8_intact(self):
        name = 'a.jsonl'
        record = count(333)
        split = record.index('测'.encode('utf-8')) + 1
        totals, offsets = self.run_polls({name: b''}, [
            {name: START + record[:split]}, {name: record[split:] + END},
        ])
        self.assertEqual(totals, [333])
        self.assertEqual(offsets[1][name], len(START))
        self.assertEqual(offsets[2][name], len(START + record + END))

    def test_existing_log_skips_history_and_retains_incomplete_tail(self):
        name = 'a.jsonl'
        history = START + count(111) + END
        record = count(333)
        totals, offsets = self.run_polls({name: history + record[:-1]}, [
            {name: record[-1:] + END},
        ])
        self.assertEqual(offsets[0][name], len(history))
        self.assertEqual(totals, [333])

    def test_resumes_saved_byte_offset_in_windows_log(self):
        name = 'a.jsonl'
        history = (START + count(111) + END).replace(b'\n', b'\r\n')
        new_turn = (START + count(222) + END).replace(b'\n', b'\r\n')
        totals, offsets = self.run_polls({name: history + new_turn}, [],
                                         saved_offsets={name: len(history)})
        self.assertEqual(totals, [222])
        self.assertEqual(offsets[-1][name], len(history + new_turn))


class QuotaTests(unittest.TestCase):
    @staticmethod
    def state(reset, used=60):
        return {'rate_limits': {'primary': {
            'used_percent': used, 'window_minutes': 300, 'resets_at': reset,
        }}}

    @patch.object(watch.time, 'time', return_value=2_000_000_000)
    def test_unknown_or_expired_reset_shows_pending(self, clock):
        for reset in (None, 'invalid', clock.return_value):
            with self.subTest(reset=reset):
                state = self.state(reset)
                state['last_used'] = {'primary': 20}
                self.assertIsNone(watch.windows_snapshot(state)[0]['remain'])
                self.assertEqual(watch.float_payload(state)['reset5'], '待刷新')
                self.assertEqual(watch.tray_text(state)[0], '--')
                self.assertIn('待刷新', watch.tooltip(state))
                _, lines, label = watch.build_notification(watch.Turn(), state)
                self.assertIn('待刷新', lines[0])
                self.assertIn('（—）', label)

    @patch.object(watch.time, 'time', return_value=2_000_000_000)
    def test_fresh_snapshot_can_show_real_100_percent(self, clock):
        state = self.state(clock.return_value + 300, used=0)
        self.assertEqual(watch.tray_text(state)[0], '100')
        self.assertEqual(watch.float_payload(state)['p1'], 100.0)
        self.assertIn('剩100%', watch.tooltip(state))

    @patch.object(watch.time, 'time', return_value=2_000_000_000)
    def test_pending_window_does_not_hide_other_windows_in_details(self, clock):
        state = self.state(None)
        state['rate_limits']['secondary'] = {
            'used_percent': 5, 'window_minutes': 10080, 'resets_at': clock.return_value + 300,
        }
        self.assertEqual(watch.tray_text(state)[0], '--')
        self.assertIn('每周剩95%', watch.tooltip(state))
        self.assertEqual(watch.float_payload(state)['p2'], 95.0)

    def test_no_data_still_waits_for_first_snapshot(self):
        self.assertIn('等待数据', watch.tooltip({}))
        self.assertEqual(watch.float_payload({})['reset5'], '--:--')

    def test_takeover_requires_explicit_setting(self):
        self.assertFalse(watch.takeover_enabled({}))
        self.assertFalse(watch.takeover_enabled({'takeover': None}))
        self.assertFalse(watch.takeover_enabled({'takeover': False}))
        self.assertTrue(watch.takeover_enabled({'takeover': True}))
