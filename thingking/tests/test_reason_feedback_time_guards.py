"""Reason-version credit and raw-source time fences, on synthetic temporary stores."""
import unittest
from unittest.mock import patch

import test_memory_learning as learning


class ReasonFeedbackTimeTest(unittest.TestCase):
    def setUp(self):
        self.fx = learning.MemoryLearningTest()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.service, self.repo, self.graph = self.fx.service, self.fx.repo, self.fx.graph

    def change_reason(self, node, chain):
        revision = self.repo.conn.execute("SELECT revision FROM association_chains WHERE id=?", (chain,)).fetchone()[0]
        self.service.correct_memory(scope="owner", node_id=node["id"], operation_id="reason-change",
            expected_digest=node["digest"], kind="reason", chain_id=chain, expected_chain_revision=revision,
            replacement="围巾购买记录", explanation="并不是赠送约定")

    def test_changed_reason_cannot_inherit_old_chain_credit(self):
        node, chain, _, shown, citation = self.fx.context()
        self.service.usage.record("owner", "first", [shown], [citation], "礼物")
        self.assertGreater(self.service.usage.reward(node["id"], chain), 0)
        self.change_reason(node, chain)
        self.assertEqual(self.service.usage.reward(node["id"], chain), 0)
        # The original was actually used, independently of why it was retrieved.
        self.assertGreater(self.service.usage.reward(node["id"]), 0)

    def test_stale_or_forged_presentation_versions_cannot_reward_new_reason(self):
        node, chain, _, shown, citation = self.fx.context()
        self.change_reason(node, chain)
        forged = {**shown, "chain_versions": {chain: self.graph.reason_version(chain, "owner")}}
        self.service.usage.record("owner", "late", [forged], [citation], "礼物")
        self.assertEqual(self.service.usage.reward(node["id"], chain), 0)
        self.service.competition.mint("生日礼物", [shown])
        self.service.usage.record("owner", "fresh", [shown], [citation], "礼物")
        self.assertAlmostEqual(self.service.usage.reward(node["id"], chain), .3)

    def test_legacy_unversioned_receipts_never_claim_new_reason_credit(self):
        node, chain, _, shown, citation = self.fx.context()
        self.service.usage.record("owner", "legacy", [shown], [citation], "礼物")
        self.repo.conn.execute("UPDATE memory_usage_events SET receipt=json_remove(receipt,'$.reason_version','$.chain_versions')")
        self.assertEqual(self.service.usage.reward(node["id"], chain), 0)
        self.assertGreater(self.service.usage.reward(node["id"]), 0)

    def test_weight_reads_do_not_write_or_invalidate_other_chain_caches(self):
        node, chain, other, shown, citation = self.fx.context()
        self.service.usage.record("owner", "used", [shown], [citation], "礼物")
        before = self.repo.conn.total_changes
        with patch.object(self.graph, "_weights", wraps=self.graph._weights) as compute:
            with self.graph.reads.session():
                a = self.graph.weights(chain, "owner")
                self.graph.weights(other, "owner")
                self.assertEqual(self.graph.weights(chain, "owner"), a)
        self.assertEqual(compute.call_count, 2)
        self.assertEqual(self.repo.conn.total_changes, before)
        self.fx.clock.advance(365 * 86400)
        with self.graph.reads.session():
            after = self.graph.weights(chain, "owner")
        self.assertLess(after[node["id"]]["recall_strength"], a[node["id"]]["recall_strength"])
        self.assertEqual(self.repo.conn.total_changes, before)

    def raw(self, ts, *, archived=False):
        self.repo.ensure_conversation("original", "local", "owner")
        return self.repo.add_message("original", "owner", "user", "原始代号 QZ981", ts, "2026-01-01",
                                     origin="archived" if archived else "live")

    def test_archived_original_without_summary_stays_cold_after_first_graph_registration(self):
        mid = self.raw(self.fx.clock(), archived=True)
        self.assertIsNone(self.graph.for_message(mid))
        result = self.service.search("QZ981", ["owner"])
        self.assertEqual(result[0]["retrieval_status"], "cold")
        self.assertEqual(self.graph.for_message(mid)["retrieval_status"], "cold")
        self.service.retriever.sync()
        self.assertNotIn(f"u:{mid}:0", self.service.retriever.keys.all_ids())
        self.assertNotIn(f"u:{mid}:0", {k["id"] for k in self.service.retriever.keys.unindexed_keys()})
        self.assertFalse(self.service.search("QZ981", ["owner"], as_of=self.fx.clock() - 1))

    def test_zero_and_current_time_fence_hot_and_cold_originals(self):
        zero, current = set(), set()
        for archived in (False, True):
            for ts in (0, 1, self.fx.clock() + 1):
                mid = self.raw(ts, archived=archived)
                self.graph.register_raw([self.repo.get_message(mid)])
                if ts == 0:
                    zero.add(mid)
                if ts <= self.fx.clock():
                    current.add(mid)
        for graph_enabled in (False, True):
            old = self.service.retriever.search("QZ981", ["owner"], k=10, as_of=0, expand_graph=graph_enabled)
            self.assertEqual({hit["message_id"] for hit in old}, zero)
            now = self.service.retriever.search("QZ981", ["owner"], k=10, as_of=None, expand_graph=graph_enabled)
            self.assertEqual({hit["message_id"] for hit in now}, current)

    def test_grouped_raw_node_cannot_smuggle_a_future_message_with_an_older_hit(self):
        past = self.raw(0)
        future = self.raw(100)
        self.graph.register_raw([self.repo.get_message(past), self.repo.get_message(future)])
        self.assertFalse(self.service.search("QZ981", ["owner"], as_of=0))


if __name__ == "__main__":
    unittest.main()
