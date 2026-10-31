"""Isolated delivery ordering, poison-record recovery and foreground fairness checks."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'thingking' / 'src')]
from common.memory_journal import MemoryJournal, MemoryOutbox, delivery_error_kind
from chatBot.memory_hub import MemoryHub
from chatBot.store import MemoryStore
from memory.channels import local_envelope
from memory.lexical_index import LexicalIndex
from memory.provenance import ProvenanceStore
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings


def event(number, user='2'):
    return dict(post_type='message', self_id='99', user_id=user, message_id=number,
                message_type='private', message=f'fixture message {number}', time=1000 + number)


class OutboxRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outbox = MemoryOutbox(Path(self.temp.name) / 'outbox.db')

    def test_poison_record_isolated_and_dependent_turn_waits_for_repair(self):
        delivered = []
        def deliver(method, **params):
            if params.get('bad'):
                raise ValueError('invalid fixture')
            delivered.append(params['source_ref'])
        with patch('common.memory_journal.time.time', return_value=1000):
            self.outbox.put('bad-message', 'record_message', {'bad': True})
            self.outbox.put('dependent-turn', 'record_experience', {'record': {'source_message_refs': ['bad-message']}})
            self.outbox.put('other-conversation', 'record_message', {'content': 'valid'})
        for now in (1000, 1001, 1003, 1007, 1015):
            with patch('common.memory_journal.time.time', return_value=now):
                self.outbox.sync(deliver)
        self.assertEqual(delivered, ['other-conversation'])
        self.assertEqual(self.outbox.stats(), {'pending': 2, 'retrying': 1, 'quarantined': 1})
        restarted = MemoryOutbox(self.outbox.path)
        with patch('common.memory_journal.time.time', return_value=1016):
            self.assertEqual(restarted.sync(deliver), 0)
            restarted.put('bad-message', 'record_message', {'content': 'repaired'})
            restarted.sync(deliver)
            restarted.sync(deliver)
        self.assertEqual(delivered, ['other-conversation', 'bad-message', 'dependent-turn'])
        self.assertEqual(restarted.stats()['pending'], 0)

    def test_malformed_json_does_not_block_valid_record(self):
        self.outbox.put('broken', 'record_message', {})
        with self.outbox.connection() as db:
            db.execute("UPDATE memory_outbox SET params='broken json' WHERE source_ref='broken'")
        self.outbox.put('valid', 'record_message', {})
        delivered = []
        self.outbox.sync(lambda method, **params: delivered.append(params['source_ref']))
        self.assertEqual(delivered, ['valid'])
        self.assertEqual(self.outbox.stats()['retrying'], 1)

    def test_new_revision_during_delivery_is_not_acknowledged_as_old(self):
        self.outbox.put('source', 'record_message', {'content': 'old'})
        received = []
        def deliver(method, **params):
            received.append(params['content'])
            if params['content'] == 'old':
                self.outbox.put('source', 'record_message', {'content': 'new'})
        self.outbox.sync(deliver, limit=1)
        self.assertEqual(self.outbox.stats()['pending'], 1)
        self.outbox.sync(deliver)
        self.assertEqual(received, ['old', 'new'])

    def test_two_consumers_share_delivery_lease_without_locking_other_records(self):
        self.outbox.put('blocked', 'record_message', {})
        entered, release = threading.Event(), threading.Event()
        def slow(method, **params):
            entered.set()
            release.wait(3)
        thread = threading.Thread(target=lambda: self.outbox.sync(slow))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            other = MemoryOutbox(self.outbox.path)
            other.put('free', 'record_message', {})
            delivered = []
            other.sync(lambda method, **params: delivered.append(params['source_ref']))
            self.assertEqual(delivered, ['free'])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_new_revision_replaces_obsolete_dependencies(self):
        self.outbox.put('blocked-source', 'record_message', {})
        self.outbox.put('derived', 'record_experience', {'record': {'source_message_refs': ['blocked-source']}})
        self.outbox.put('derived', 'record_experience', {'record': {'source_message_refs': []}})
        seen = []
        self.outbox.sync(lambda method, **params: seen.append(params['source_ref']), source_refs=['derived'])
        self.assertEqual(seen, ['derived'])

    def test_batch_time_budget_limits_dispatches_without_discarding_later_rows(self):
        for key in ('first', 'second'):
            self.outbox.put(key, 'record_message', {})
        monotonic = [0.0]
        seen = []
        def deliver(method, **params):
            seen.append(params['source_ref'])
            monotonic[0] += 5
        with patch('common.memory_journal.time.monotonic', side_effect=lambda: monotonic[0]):
            self.outbox.sync(deliver, time_budget=2)
        self.assertEqual(seen, ['first'])
        self.assertEqual(self.outbox.stats()['pending'], 1)

    def test_error_classes_have_separate_retry_schedules_and_safe_migration(self):
        errors = {'validation': ValueError('invalid fixture'), 'auth_config': PermissionError('authentication required'),
                  'transient': TimeoutError('timed out'), 'unknown': RuntimeError('unexpected fixture')}
        with self.outbox.connection() as db:
            db.execute('ALTER TABLE memory_outbox_delivery DROP COLUMN error_kind')
        outbox = MemoryOutbox(self.outbox.path)
        with patch('common.memory_journal.time.time', return_value=1000):
            for kind in errors:
                outbox.put(kind, 'record_message', {})
            def failed(method, **params):
                raise errors[params['source_ref']]
            outbox.sync(failed)
        with outbox.connection() as db:
            rows = {r['source_ref']: dict(r) for r in db.execute('SELECT * FROM memory_outbox_delivery')}
        for kind in errors:
            self.assertEqual(rows[kind]['error_kind'], kind)
            self.assertEqual(rows[kind]['quarantined'], int(kind in {'validation', 'auth_config'}))
            self.assertEqual(rows[kind]['next_attempt'], 1300 if kind in {'validation', 'auth_config'} else 1001)
        self.assertEqual(delivery_error_kind(RuntimeError('Memory Server record_message failed: ValueError: bad')), 'validation')
        self.assertEqual(delivery_error_kind(RuntimeError('invalid_request')), 'validation')

    def test_missing_local_source_waits_until_written_but_filtered_source_is_explicitly_omitted(self):
        ref = self.outbox.local_sources[:-1] + 'desktop:fixture:user'
        self.outbox.put('derived', 'record_experience', {'record': {'source_message_refs': [ref]}})
        seen = []
        deliver = lambda method, **params: seen.append(params['source_ref'])
        self.assertEqual(self.outbox.sync(deliver), 0)
        self.outbox.put(ref, 'record_message', {'content': 'late raw source'})
        self.outbox.sync(deliver)
        self.assertEqual(seen, [ref, 'derived'])
        omitted = self.outbox.local_sources[:-1] + 'desktop:empty:user'
        self.outbox.record_message({}, 'user', '', omitted)
        self.outbox.put('empty-turn', 'record_experience', {'record': {'source_message_refs': [omitted]}})
        self.outbox.sync(deliver)
        self.assertEqual(seen[-1], 'empty-turn')


class ProvenanceDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = MemoryRepository(':memory:')
        self.addCleanup(self.repo.close)
        self.service = MemoryService(self.repo, LexicalIndex(self.repo),
            MemorySettings(data_dir=Path(self.temp.name), state_updates=False))
        self.outbox = MemoryOutbox(Path(self.temp.name) / 'desktop.db')
        self.envelope = local_envelope('fixture').to_dict()
        self.journal = MemoryJournal(self.outbox, self.envelope, 'desktop:fixture')
        self.ref = self.journal.prefix + ':user'
        self.journal.source_message_refs = [self.ref]
        self.journal.turn_message_refs = [self.ref]

    def assert_same_event(self):
        mid = self.repo.conn.execute('SELECT id FROM messages WHERE source_ref=?', (self.ref,)).fetchone()[0]
        tool = self.repo.conn.execute("SELECT memory_id FROM experiences WHERE json_extract(record,'$.category')='tool'").fetchone()[0]
        raw = self.service.graph.for_message(mid)
        tool = self.service.graph.for_memory(tool)
        self.assertFalse(self.service.graph.independent(raw, tool))
        return mid

    def records(self):
        self.journal.capture('web_fetch', {}, 'fixture external observation', category='tool', status='succeeded')
        self.journal._record(self.journal.prefix + ':turn', {'action': 'dialogue_turn', 'category': 'turn',
                                                          'status': 'completed', 'result': ''})

    def deliver_unordered(self):
        # Legacy/independent transports can still deliver a journal before its
        # source. Exercise the service repair independently of the new queue.
        with self.outbox.connection() as db:
            rows = db.execute('SELECT method,params FROM memory_outbox ORDER BY updated,source_ref').fetchall()
        for row in rows:
            getattr(self.service, row['method'])(**json.loads(row['params']))

    def test_desktop_finally_update_keeps_message_before_derived_records(self):
        self.outbox.record_message(self.envelope, 'user', 'fixture evidence', self.ref, state_update=False,
                                   turn_ref=self.journal.prefix + ':turn')
        self.records()
        self.outbox.record_message(self.envelope, 'user', 'fixture evidence', self.ref, state_update=True,
                                   turn_ref=self.journal.prefix + ':turn')
        delivered = []
        def deliver(method, **params):
            delivered.append(method)
            return getattr(self.service, method)(**params)
        for _ in range(4):
            self.outbox.sync(deliver)
        self.assertEqual(delivered[0], 'record_message')
        self.assert_same_event()

    def test_late_message_binds_even_if_transport_reorders_records(self):
        self.records()
        self.deliver_unordered()
        self.service.record_message(self.envelope, 'user', 'fixture evidence', source_ref=self.ref,
                                    turn_ref=self.journal.prefix + ':turn')
        mid = self.assert_same_event()
        self.service.provenance.register_message('other-scope', self.ref, mid, self.journal.prefix + ':turn')
        self.assertIsNone(self.repo.conn.execute("SELECT 1 FROM evidence_events WHERE scope='other-scope'").fetchone())

    def test_historical_gap_repaired_by_resumable_cursor(self):
        self.test_late_message_binds_even_if_transport_reorders_records()
        mid = self.repo.conn.execute('SELECT id FROM messages WHERE source_ref=?', (self.ref,)).fetchone()[0]
        self.repo.conn.execute('DELETE FROM evidence_event_refs')
        self.repo.conn.execute('DELETE FROM evidence_events WHERE root=?', (f'message:{mid}',))
        self.assertEqual(self.service.provenance.reconcile(limit=1), 1)
        restarted = ProvenanceStore(self.repo)
        self.assertEqual(restarted.reconcile(limit=1), 1)
        self.assertEqual(restarted.reconcile(limit=1), 0)
        self.assert_same_event()

    def test_native_operation_subkey_shares_its_turn_event(self):
        self.journal._record(self.journal.prefix + ':native:desktop:fixture:tool:0',
                            {'action': 'send_private_msg', 'category': 'tool', 'status': 'succeeded', 'result': 'fixture receipt'})
        self.journal._record(self.journal.prefix + ':turn', {'action': 'dialogue_turn', 'category': 'turn',
                                                          'status': 'completed', 'result': ''})
        self.deliver_unordered()
        self.service.record_message(self.envelope, 'user', 'fixture evidence', source_ref=self.ref,
                                    turn_ref=self.journal.prefix + ':turn')
        self.assert_same_event()

    def test_longer_turn_prefix_cannot_be_registered_as_shorter_turn(self):
        ref = self.journal.prefix + ':another-turn:receipt'
        self.assertIsNone(self.service.provenance.register('owner', ref, {'turn_id': self.journal.turn_id}))


class GatewayForeground(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / 'gateway.db', owners={'1'})
        self.calls = []
        outer = self
        class Client:
            def call(self, method, **params):
                outer.calls.append((method, params))
                return {'method': method}
        self.hub = MemoryHub(self.store, client=Client())

    def test_context_syncs_current_conversation_not_other_backlog(self):
        for number in range(80):
            self.store.ingest(event(number, '3'))
        current = event(100, '2')
        self.store.ingest(current)
        self.hub.call(self.store.identity(current), 'build_context', {})
        source_calls = [p for method, p in self.calls if method == 'record_message']
        self.assertEqual(len(source_calls), 1)
        self.assertEqual(source_calls[0]['content'], 'fixture message 100')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE memory_synced=0').fetchone()[0], 80)
        self.hub.sync()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE memory_synced=0').fetchone()[0], 0)

    def test_background_remote_call_does_not_hold_global_foreground_lock(self):
        self.store.ingest(event(1, '3'))
        entered, release = threading.Event(), threading.Event()
        original = self.hub.client.call
        def call(method, **params):
            if method == 'record_message' and params['content'] == 'fixture message 1':
                entered.set()
                release.wait(3)
            return original(method, **params)
        self.hub.client.call = call
        background = threading.Thread(target=self.hub.sync)
        background.start()
        try:
            self.assertTrue(entered.wait(1))
            current = event(2, '2')
            self.store.ingest(current)
            foreground = threading.Thread(target=lambda: self.hub.call(self.store.identity(current), 'build_context', {}))
            foreground.start()
            foreground.join(1)
            self.assertFalse(foreground.is_alive())
            self.assertTrue(background.is_alive())
        finally:
            release.set()
            background.join(3)

    def test_failed_qq_source_does_not_block_other_sources_and_can_retry(self):
        self.store.ingest(event(1, '3'))
        self.store.ingest(event(2, '2'))
        original = self.hub.client.call
        def call(method, **params):
            if method == 'record_message' and params['content'] == 'fixture message 1':
                raise ValueError('fixture rejection')
            return original(method, **params)
        self.hub.client.call = call
        self.hub.sync()
        with self.store.connection() as db:
            states = dict(db.execute('SELECT content,memory_synced FROM messages'))
        self.assertEqual(states, {'fixture message 1': 0, 'fixture message 2': 1})
        self.hub.client.call = original
        self.hub.outbox.retry(self.hub.source + ':1')
        self.hub.sync()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE memory_synced=0').fetchone()[0], 0)

    def test_old_recall_in_visible_scope_is_synced_before_context(self):
        for number in range(50):
            self.store.ingest(event(number))
        self.hub.sync()
        self.calls.clear()
        self.store.ingest(dict(post_type='notice', notice_type='friend_recall', self_id='99', user_id='2', message_id=0))
        self.hub.call(self.store.identity(event(49)), 'build_context', {})
        source_calls = [p for method, p in self.calls if method == 'record_message']
        self.assertEqual(len(source_calls), 1)
        self.assertTrue(source_calls[0]['recalled'])
        self.assertEqual(self.calls[-1][0], 'build_context')


if __name__ == '__main__':
    unittest.main()
