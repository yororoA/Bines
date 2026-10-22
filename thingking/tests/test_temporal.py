"""Offline tests using temporary SQLite databases, without models or services."""
import itertools
import tempfile
import unittest
from pathlib import Path

from helpers import Clock
from memory.repository import MemoryRepository, StateConflict
from memory.temporal import TemporalStore


class TemporalTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bines_temporal_")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.db"
        self.clock = Clock(100)
        self.repo = MemoryRepository(self.path, clock=self.clock)
        self.addCleanup(self.repo.close)
        self.original_tables = [
            row["name"] for row in self.repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        self.store = TemporalStore(self.repo)

    def memory(self, memory_id, scope="owner", source_ref=None, created_at=1):
        return self.repo.add_memory(
            "semantic", scope, "body:" + memory_id, memory_id=memory_id,
            source_ref=source_ref, created_at=created_at,
        )

    def fact(self, memory_id, at, value=None, scope="owner", subject="user", predicate="location", source_ref=None):
        self.memory(memory_id, scope)
        self.store.put_fact(
            memory_id, scope, subject, predicate, memory_id if value is None else value, at, source_ref,
        )
        return memory_id

    def fact_ids(self, at=None, scope="owner", **filters):
        return [row["memory_id"] for row in self.store.facts(scope, as_of=at, **filters)]

    def original_rows(self):
        return {name: [tuple(row) for row in self.repo.conn.execute(f'SELECT * FROM "{name}"')]
                for name in self.original_tables}


class StateHistoryTest(TemporalTestCase):
    def test_baseline_is_one_observation_not_invented_history(self):
        self.repo.put_state("owner", {"score": 5})
        self.clock.advance(10)
        self.repo.put_state("owner", {"score": 6})
        self.repo.put_state("qq_group:1:user:2", {"quest": "local"})
        before = self.original_rows()
        self.clock.advance(10)
        store = TemporalStore(self.repo)
        self.assertEqual(self.original_rows(), before)
        baseline = store.state_history("owner")[0]
        self.assertEqual(baseline["version"], 2)
        self.assertEqual(baseline["data"], {"score": 6})
        self.assertTrue(baseline["is_baseline"])
        self.assertEqual(baseline["effective_at"], 110)
        self.assertEqual(baseline["recorded_at"], 120)
        self.assertEqual(store.state_history("owner", as_of=109), [])
        self.assertEqual(store.facts("owner"), [])
        self.repo.put_state("owner", {"score": 7})
        self.clock.advance(10)
        self.assertEqual(TemporalStore(self.repo).state_history("owner"), [baseline])
        self.assertEqual(store.state_history("missing"), [])
        self.assertEqual(len(store.state_history("qq_group:1:user:2")), 1)

    def test_initializer_does_not_commit_outer_transaction(self):
        repo = MemoryRepository(":memory:", clock=self.clock)
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with repo.transaction():
                repo.put_state("owner", {"pending": True})
                TemporalStore(repo)
                self.assertTrue(repo.conn.in_transaction)
                raise RuntimeError("abort")
        self.assertEqual(repo.get_state("owner")["version"], 0)
        self.assertIsNone(repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'state_history'"
        ).fetchone())

    def test_snapshot_retry_is_immutable_and_json_key_order_independent(self):
        data = {"score": 0, "nested": {"b": [1, False, None], "a": "\u5730\u70b9"}}
        self.assertTrue(self.store.snapshot_state("owner", 0, data, 0, source_ref="event:1"))
        reordered = {"nested": {"a": "\u5730\u70b9", "b": [1, False, None]}, "score": 0}
        self.assertFalse(self.store.snapshot_state("owner", 0, reordered, 0, source_ref="event:1"))
        expected = self.store.state_history("owner")
        for changes in ({"data": {}}, {"recorded_at": 1}, {"effective_at": 1}, {"source_ref": "event:2"}):
            with self.subTest(changes=changes):
                args = dict(scope="owner", version=0, data=data, recorded_at=0, source_ref="event:1")
                args.update(changes)
                with self.assertRaises(StateConflict):
                    self.store.snapshot_state(**args)
                self.assertEqual(self.store.state_history("owner"), expected)
        data["nested"]["b"].append("mutation")
        self.assertEqual(self.store.state_history("owner")[0]["data"], reordered)
        self.assertEqual(self.repo.get_state("owner")["version"], 0)

    def test_history_uses_effective_time_and_exact_scope(self):
        self.store.snapshot_state("owner", 1, {"n": 1}, 50, effective_at=10)
        self.store.snapshot_state("owner", 3, {"n": 3}, 80, effective_at=30)
        self.store.snapshot_state("owner", 2, {"n": 2}, 90, effective_at=20)
        self.store.snapshot_state("owner", 4, {"n": 4}, 100, effective_at=200)
        for scope in ("qq_private:1", "qq_group:1", "qq_group:1:user:2", "qq_group:1:user:3"):
            self.store.snapshot_state(scope, 1, {"scope": scope}, 10)
            self.assertEqual([h["data"] for h in self.store.state_history(scope)], [{"scope": scope}])
        self.assertEqual([h["version"] for h in self.store.state_history("owner")], [3, 2, 1])
        self.assertEqual([h["version"] for h in self.store.state_history("owner", as_of=20)], [2, 1])
        self.assertEqual(self.store.state_history("owner", as_of=0), [])
        self.assertEqual([h["version"] for h in self.store.state_history("owner", limit=1)], [3])
        self.assertEqual(self.store.state_history("owner", limit=0), [])
        self.assertEqual(self.store.state_history("owner", as_of=200)[0]["version"], 4)

    def test_equal_effective_times_order_by_version(self):
        for version in (1, 3, 2):
            self.store.snapshot_state("owner", version, {"n": version}, 10)
        self.assertEqual([h["version"] for h in self.store.state_history("owner")], [3, 2, 1])

    def test_snapshot_source_cannot_cross_scope_or_reference_deleted_memory(self):
        self.memory("source", "owner", source_ref="event:1")
        for ref in ("source", "event:1"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.store.snapshot_state("qq_group:1", 1, {"private": True}, 10, source_ref=ref)
        self.repo.delete_memory("source")
        with self.assertRaises(ValueError):
            self.store.snapshot_state("owner", 1, {}, 10, source_ref="source")
        self.assertEqual(self.store.state_history("owner"), [])


class FactVersionTest(TemporalTestCase):
    def test_latest_historical_future_and_half_open_boundaries(self):
        self.fact("old", 0, value={"place": "home"})
        self.fact("current", 20, value=["office", False, 0])
        self.fact("future", 200)
        self.assertEqual(self.fact_ids(-1), [])
        self.assertEqual(self.fact_ids(0), ["old"])
        self.assertEqual(self.fact_ids(19.999), ["old"])
        self.assertEqual(self.fact_ids(20), ["current"])
        self.assertEqual(self.fact_ids(), ["current"])
        self.assertEqual(self.fact_ids(200), ["future"])
        old = self.store.facts("owner", as_of=0)[0]
        self.assertEqual(old["value"], {"place": "home"})
        self.assertEqual(old["valid_to"], 20)
        self.assertEqual(old["recorded_at"], 100)
        self.assertEqual(old["source_memory_id"], "old")
        self.assertEqual(self.store.facts("owner")[0]["value"], ["office", False, 0])

    def test_all_arrival_orders_produce_the_same_timeline(self):
        for order in itertools.permutations((10, 20, 30, 40)):
            with self.subTest(order=order):
                scope = "order:" + ",".join(map(str, order))
                for at in order:
                    self.fact(f"{scope}:{at}", at, scope=scope)
                rows = self.repo.conn.execute(
                    "SELECT valid_from, valid_to FROM fact_versions WHERE scope = ? ORDER BY valid_from", (scope,),
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(10, 20), (20, 30), (30, 40), (40, None)])
                for at in (10, 19, 20, 39, 40, 100):
                    expected = max(start for start in order if start <= at)
                    self.assertEqual(self.fact_ids(at, scope), [f"{scope}:{expected}"])

    def test_scope_subject_and_predicate_are_independent_keys(self):
        self.fact("owner-old", 10)
        self.fact("group", 20, scope="qq_group:1")
        self.fact("private", 30, scope="qq_private:2")
        self.fact("other-user", 40, subject="other")
        self.fact("other-predicate", 50, predicate="preference")
        self.fact("owner-new", 60)
        self.assertEqual(self.fact_ids(subject="user", predicate="location"), ["owner-new"])
        self.assertEqual(self.fact_ids(scope="qq_group:1"), ["group"])
        self.assertEqual(self.fact_ids(scope="qq_private:2"), ["private"])
        self.assertEqual(self.fact_ids(subject="other"), ["other-user"])
        self.assertEqual(self.fact_ids(predicate="preference"), ["other-predicate"])
        self.assertEqual(self.fact_ids(10, predicate="location"), ["owner-old"])
        self.assertEqual(self.fact_ids(limit=1), ["owner-new"])
        self.assertEqual(self.fact_ids(limit=0), [])
        self.assertEqual(self.fact_ids(scope="owner' OR 1=1 --"), [])

    def test_fact_retry_preserves_recording_time_and_closed_interval(self):
        self.fact("old", 10, value={"b": 2, "a": 1}, source_ref="event:1")
        self.fact("new", 20)
        self.clock.advance(100)
        self.assertFalse(self.store.put_fact("old", "owner", "user", "location", {"a": 1, "b": 2}, 10, "event:1"))
        row = self.store.facts("owner", as_of=10)[0]
        self.assertEqual(row["recorded_at"], 100)
        self.assertEqual(row["valid_to"], 20)
        for changes in ({"subject": "other"}, {"predicate": "other"}, {"value": "other"},
                        {"valid_from": 11}, {"source_ref": "event:2"}):
            with self.subTest(changes=changes):
                args = dict(memory_id="old", scope="owner", subject="user", predicate="location",
                            value={"a": 1, "b": 2}, valid_from=10, source_ref="event:1")
                args.update(changes)
                with self.assertRaises(StateConflict):
                    self.store.put_fact(**args)
                self.assertEqual(self.store.facts("owner", as_of=10)[0], row)

    def test_same_key_and_time_conflict_does_not_close_existing_version(self):
        self.fact("first", 10)
        self.memory("second")
        with self.assertRaises(StateConflict):
            self.store.put_fact("second", "owner", "user", "location", "second", 10)
        self.assertEqual(self.fact_ids(), ["first"])
        self.assertIsNone(self.store.facts("owner")[0]["valid_to"])

    def test_parent_and_source_must_be_active_and_scope_matched(self):
        self.fact("valid", 10)
        self.memory("group", scope="qq_group:1", source_ref="group-event")
        self.memory("deleted")
        self.repo.delete_memory("deleted")
        self.memory("candidate")
        for memory_id in ("missing", "deleted", "group"):
            with self.subTest(memory_id=memory_id):
                with self.assertRaises(ValueError):
                    self.store.put_fact(memory_id, "owner", "user", "location", "invalid", 20)
        for ref in ("group", "group-event", "deleted"):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.store.put_fact("candidate", "owner", "user", "location", "invalid", 20, ref)
        self.assertEqual(self.fact_ids(), ["valid"])
        self.assertIsNone(self.store.facts("owner")[0]["valid_to"])

    def test_values_round_trip_without_string_coercion(self):
        for index, value in enumerate((None, False, 0, "", ["a", 2], {"nested": [None]})):
            memory_id = self.memory(f"json:{index}")
            self.store.put_fact(memory_id, "owner", "user", f"field:{index}", value, 0)
            self.assertEqual(self.store.facts("owner", predicate=f"field:{index}")[0]["value"], value)

    def test_all_operations_leave_repository_owned_rows_unchanged(self):
        self.memory("memory", source_ref="source")
        self.repo.put_state("owner", {"sensitive": True})
        before = self.original_rows()
        TemporalStore(self.repo)
        self.store.snapshot_state("owner", 2, {"sensitive": True}, 100, source_ref="source")
        self.store.put_fact("memory", "owner", "user", "location", "private", 10)
        self.store.delete_memory("memory")
        self.assertEqual(self.original_rows(), before)

    def test_writes_participate_in_caller_transaction_rollback(self):
        self.fact("old", 10)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.repo.transaction():
                self.fact("new", 20)
                self.store.snapshot_state("owner", 1, {}, 20)
                self.store.delete_memory("old")
                raise RuntimeError("abort")
        self.assertEqual(self.fact_ids(), ["old"])
        self.assertIsNone(self.store.facts("owner")[0]["valid_to"])
        self.assertEqual(self.store.state_history("owner"), [])
        self.assertIsNone(self.repo.get_memory("new"))
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM temporal_retractions").fetchone()[0], 0)


class VisibilityTest(TemporalTestCase):
    def test_candidate_filter_keeps_order_and_history_never_includes_future(self):
        self.fact("old", 10)
        self.fact("current", 20)
        self.fact("future", 200)
        self.memory("legacy", created_at=15)
        self.memory("future-legacy", created_at=200)
        self.memory("deleted")
        self.repo.delete_memory("deleted")
        candidates = ["legacy", "old", "missing", "current", "current", "future", "future-legacy", "deleted"]
        self.assertEqual(self.store.visible_memory_ids(candidates), ["legacy", "current"])
        self.assertEqual(self.store.visible_memory_ids(candidates, as_of=10), ["old"])
        self.assertEqual(self.store.visible_memory_ids(candidates, include_history=True), ["legacy", "old", "current"])
        self.assertEqual(self.store.visible_memory_ids(candidates, as_of=0, include_history=True), [])
        self.assertEqual(self.store.visible_memory_ids([]), [])

    def test_parent_deletion_hides_every_historical_query(self):
        self.fact("old", 10)
        self.fact("new", 20)
        self.repo.delete_memory("old")
        for historical in (False, True):
            self.assertEqual(self.store.visible_memory_ids(["old"], as_of=10, include_history=historical), [])
        self.assertEqual(self.fact_ids(10), [])
        self.assertEqual(self.fact_ids(), ["new"])
        self.repo.conn.execute("DELETE FROM memories WHERE id = 'new'")
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.store.visible_memory_ids(["new"], include_history=True), [])

    def test_moved_parent_scope_never_makes_old_fact_visible(self):
        self.fact("moved", 10)
        self.repo.conn.execute("UPDATE memories SET scope = 'qq_group:1' WHERE id = 'moved'")
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.fact_ids(scope="qq_group:1"), [])
        self.assertEqual(self.store.visible_memory_ids(["moved"], include_history=True), [])

    def test_large_candidate_batches_are_bounded_and_stable(self):
        with self.repo.transaction():
            ids = [self.memory(f"memory:{index}") for index in range(1205)]
        ids.reverse()
        self.assertEqual(self.store.visible_memory_ids(ids + ids), ids)

    def test_deleted_or_missing_source_hides_fact_and_inherited_state(self):
        self.memory("source", source_ref="source-event")
        self.fact("derived", 10, source_ref="source-event")
        self.store.snapshot_state("owner", 1, {"clean": True}, 1)
        self.store.snapshot_state("owner", 2, {"private": True}, 10, source_ref="source")
        self.store.snapshot_state("owner", 3, {"private": True, "other": 1}, 20)
        self.repo.delete_memory("source")
        for at in (10, 100):
            self.assertEqual(self.fact_ids(at), [])
            self.assertEqual(self.store.visible_memory_ids(["derived"], at, include_history=True), [])
        self.assertEqual([row["version"] for row in self.store.state_history("owner")], [1])
        self.repo.conn.execute("DELETE FROM memories WHERE id = 'source'")
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual([row["version"] for row in self.store.state_history("owner")], [1])


class RetractionTest(TemporalTestCase):
    def test_delete_purges_content_blocks_retry_and_does_not_revive_predecessor(self):
        self.fact("old", 10)
        self.fact("new", 20, value="sensitive")
        self.assertTrue(self.store.delete_memory("new"))
        self.assertFalse(self.store.delete_memory("new"))
        self.assertFalse(self.store.delete_memory("missing"))
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.fact_ids(10), ["old"])
        self.assertEqual(self.fact_ids(20), [])
        self.assertEqual(self.store.visible_memory_ids(["new"], include_history=True), [])
        self.assertIsNone(self.repo.conn.execute("SELECT * FROM fact_versions WHERE memory_id = 'new'").fetchone())
        self.assertNotIn("sensitive", str([tuple(row) for row in self.repo.conn.execute("SELECT * FROM temporal_retractions")]))
        with self.assertRaises(ValueError):
            self.store.put_fact("new", "owner", "user", "location", "sensitive", 20)
        self.assertFalse(self.repo.get_memory("new")["deleted"], "the integrator owns parent deletion")

    def test_late_insert_cannot_bridge_a_retracted_transition(self):
        self.fact("first", 10)
        self.fact("retracted", 20)
        self.fact("last", 40)
        self.store.delete_memory("retracted")
        self.fact("late-before", 15)
        self.assertEqual(self.fact_ids(19), ["late-before"])
        self.assertEqual(self.fact_ids(20), [])
        self.assertEqual(self.fact_ids(39), [])
        self.assertEqual(self.fact_ids(40), ["last"])
        self.fact("new-evidence", 30)
        self.assertEqual(self.fact_ids(29), [])
        self.assertEqual(self.fact_ids(30), ["new-evidence"])
        self.assertEqual(self.store.facts("owner", as_of=15)[0]["valid_to"], 20)

    def test_explicit_and_parent_sources_are_both_purged_without_crossing_scopes(self):
        self.memory("target", source_ref="parent-event")
        self.store.put_fact("target", "owner", "user", "location", "private", 10, "external-event")
        self.fact("group", 10, scope="qq_group:1", source_ref="external-event")
        self.store.snapshot_state("owner", 1, {"private": 1}, 10, source_ref="parent-event")
        self.store.snapshot_state("owner", 2, {"private": 2}, 20, source_ref="external-event")
        self.store.snapshot_state("qq_group:1", 1, {"public": 1}, 10, source_ref="external-event")
        self.store.delete_memory("target")
        self.assertEqual(self.store.state_history("owner"), [])
        self.assertEqual(self.fact_ids(scope="qq_group:1"), ["group"])
        self.assertEqual(self.store.state_history("qq_group:1")[0]["data"], {"public": 1})
        self.memory("retry")
        with self.assertRaises(ValueError):
            self.store.put_fact("retry", "owner", "user", "location", "private", 10, "external-event")

    def test_derived_fact_sources_are_purged_transitively(self):
        self.memory("source", source_ref="event")
        self.fact("child", 10, predicate="first", source_ref="source")
        self.fact("grandchild", 20, predicate="second", source_ref="child")
        self.store.snapshot_state("owner", 1, {"private": True}, 20, source_ref="grandchild")
        with self.repo.transaction():
            self.repo.delete_memory("source")
            self.store.delete_memory("source")
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.store.state_history("owner"), [])
        self.assertEqual(self.store.visible_memory_ids(["source", "child", "grandchild"], include_history=True), [])
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) FROM fact_versions").fetchone()[0], 0)

    def test_retracting_shared_source_covers_both_id_and_source_ref_aliases(self):
        self.memory("source", source_ref="source-event")
        self.fact("by-event", 10, predicate="first", source_ref="source-event")
        self.fact("by-id", 20, predicate="second", source_ref="source")
        self.store.snapshot_state("owner", 1, {"private": True}, 10, source_ref="source")
        self.store.delete_memory("by-event")
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.store.state_history("owner"), [])
        self.assertEqual(self.store.visible_memory_ids(["source", "by-id"], include_history=True), [])
        self.memory("retry")
        for source_ref in ("source", "source-event"):
            with self.subTest(source_ref=source_ref), self.assertRaises(ValueError):
                self.store.put_fact("retry", "owner", "user", "location", "private", 20, source_ref)

    def test_late_state_cannot_fill_a_gap_inside_redacted_versions(self):
        self.memory("source")
        self.store.snapshot_state("owner", 2, {"secret": True}, 20, source_ref="source")
        self.store.snapshot_state("owner", 4, {"secret": True}, 40)
        self.store.delete_memory("source")
        with self.assertRaises(StateConflict):
            self.store.snapshot_state("owner", 3, {"secret": True}, 30)
        self.assertEqual(self.store.state_history("owner"), [])
        self.assertTrue(self.store.snapshot_state("owner", 1, {"before": True}, 10))
        self.assertTrue(self.store.snapshot_state("owner", 5, {"cleaned": True}, 50))

    def test_state_redaction_removes_whole_inherited_snapshots_and_blocks_replay(self):
        self.memory("source")
        self.store.snapshot_state("owner", 1, {"safe": 1}, 1)
        self.store.snapshot_state("owner", 2, {"secret": "erase-me"}, 20, source_ref="source")
        self.store.snapshot_state("owner", 3, {"secret": "erase-me", "safe": 2}, 30, effective_at=10)
        self.store.snapshot_state("owner", 4, {"secret": "erase-me"}, 100, effective_at=200)
        self.store.delete_memory("source")
        self.assertEqual([h["version"] for h in self.store.state_history("owner", as_of=300)], [1])
        rows = self.repo.conn.execute("SELECT * FROM state_history WHERE version >= 2").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["data"] is None and row["redacted"] == 1 for row in rows))
        self.assertNotIn("erase-me", str([tuple(row) for row in rows]))
        with self.assertRaises(StateConflict):
            self.store.snapshot_state("owner", 3, {"secret": "erase-me", "safe": 2}, 30, effective_at=10)
        with self.assertRaises(ValueError):
            self.store.snapshot_state("owner", 5, {"secret": "erase-me"}, 100, source_ref="source")
        self.assertTrue(self.store.snapshot_state("owner", 5, {"cleaned": True}, 100))
        self.assertEqual(self.store.state_history("owner")[0]["data"], {"cleaned": True})

    def test_reopen_does_not_reimport_redacted_current_state(self):
        self.memory("source")
        self.repo.put_state("owner", {"secret": "still-in-current-state"})
        self.store.snapshot_state("owner", 1, {"secret": "still-in-current-state"}, 100, source_ref="source")
        self.store.delete_memory("source")
        repo = MemoryRepository(self.path, clock=self.clock)
        self.addCleanup(repo.close)
        store = TemporalStore(repo)
        self.assertEqual(store.state_history("owner"), [])
        self.assertEqual(store.visible_memory_ids(["source"], include_history=True), [])
        self.assertEqual(repo.get_state("owner")["data"], {"secret": "still-in-current-state"})

    def test_reopen_cannot_create_first_baseline_in_retracted_scope(self):
        self.memory("source")
        self.repo.put_state("owner", {"untracked": "sensitive"})
        self.store.delete_memory("source")
        self.assertEqual(TemporalStore(self.repo).state_history("owner"), [])

    def test_unlinked_baseline_cannot_be_attributed_to_a_deleted_source(self):
        self.memory("source")
        self.repo.put_state("owner", {"unattributed": 1})
        TemporalStore(self.repo)
        self.store.delete_memory("source")
        rows = self.store.state_history("owner")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_baseline"])


class ValidationTest(TemporalTestCase):
    def test_invalid_times_never_modify_history(self):
        self.memory("memory")
        for at in (float("nan"), float("inf"), -float("inf"), True, "10", 10 ** 1000):
            with self.subTest(at=repr(at)):
                with self.assertRaises(ValueError):
                    self.store.snapshot_state("owner", 1, {}, at)
                with self.assertRaises(ValueError):
                    self.store.snapshot_state("owner", 1, {}, 0, effective_at=at)
                with self.assertRaises(ValueError):
                    self.store.put_fact("memory", "owner", "user", "location", "value", at)
                for read in (lambda: self.store.facts("owner", as_of=at),
                             lambda: self.store.state_history("owner", as_of=at),
                             lambda: self.store.visible_memory_ids([], as_of=at)):
                    with self.assertRaises(ValueError):
                        read()
        self.assertEqual(self.fact_ids(), [])
        self.assertEqual(self.store.state_history("owner"), [])

    def test_invalid_keys_versions_limits_and_json_are_rejected(self):
        self.memory("memory")
        for version in (-1, 1.5, True, "1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.store.snapshot_state("owner", version, {}, 0)
        for limit in (-1, 1.5, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.store.facts("owner", limit=limit)
                with self.assertRaises(ValueError):
                    self.store.state_history("owner", limit=limit)
        with self.assertRaises(ValueError):
            self.store.snapshot_state("owner", 1, [], 0)
        with self.assertRaises(ValueError):
            self.store.snapshot_state("", 1, {}, 0)
        with self.assertRaises(ValueError):
            self.store.put_fact("memory", "owner", "", "location", "value", 0)
        for value in (float("nan"), {"bad": float("inf")}, object()):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.store.put_fact("memory", "owner", "user", "location", value, 0)
        self.assertEqual(self.fact_ids(), [])


if __name__ == "__main__":
    unittest.main()
