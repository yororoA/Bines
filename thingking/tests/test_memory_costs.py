"""Actual request accounting includes retries, failures and interrupted calls."""
import unittest

from common.model_usage import capture_model_usage, measured_request
from memory.costs import ModelCostStore
from memory.repository import MemoryRepository
from test_model_completion import model_with_responses, response


class ModelCostsTest(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository(":memory:")
        self.store = ModelCostStore(self.repo)
        jid = self.repo.enqueue_job("summarize", "owner", {}, "cost-test")
        self.job = self.repo.claim_job(300)
        self.assertEqual(jid, self.job["id"])

    def tearDown(self):
        self.repo.close()

    def sink(self, event):
        return self.store.record(self.job, self.job["attempts"], event)

    def test_truncation_retry_counts_both_actual_calls_and_reasoning_is_not_added_twice(self):
        first, second = response(finish_reason="length"), response("完成")
        for item in (first, second):
            item.usage.prompt_tokens = 100
            item.usage.completion_tokens = 50
            item.usage.completion_tokens_details.reasoning_tokens = 30
        with capture_model_usage(self.sink):
            self.assertEqual(model_with_responses(first, second).complete_text([]), "完成")
        stats = self.store.stats()[0]
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["prompt_tokens"], 200)
        self.assertEqual(stats["completion_tokens"], 100)
        self.assertEqual(stats["reasoning_tokens"], 60)
        self.assertEqual(stats["truncated_calls"], 1)
        self.assertEqual(stats["unknown_usage_calls"], 0)

    def test_error_and_missing_usage_are_unknown_and_events_are_idempotent(self):
        def failure(**kwargs):
            raise TimeoutError("unavailable")
        with capture_model_usage(self.sink):
            with self.assertRaises(TimeoutError):
                measured_request(failure, model="test")
            measured_request(lambda **kwargs: response("没有完整账单"), model="test")
        stats = self.store.stats()[0]
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["unknown_usage_calls"], 2)
        self.assertEqual(stats["error_calls"], 1)
        event = {"call_id": "a" * 32, "model": "test", "status": "started", "started_at": 1}
        self.sink(event)
        self.sink(event)
        self.sink({**event, "status": "returned", "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        self.sink(event)  # A late start cannot replace recorded usage.
        self.assertEqual(self.store.stats()[0]["calls"], 3)
        self.assertEqual(self.store.stats()[0]["prompt_tokens"], 5)
        self.assertEqual(self.store.stats()[0]["unknown_usage_calls"], 2)

    def test_crashed_process_leaves_visible_unknown_cost(self):
        self.sink({"call_id": "b" * 32, "status": "started", "started_at": 1})
        self.assertEqual(self.store.stats()[0]["unknown_usage_calls"], 1)

    def test_start_event_is_not_mutated_into_a_duplicate_final_event(self):
        events = []
        with capture_model_usage(events.append):
            measured_request(lambda **kwargs: response("完成"), model="test")
        self.assertEqual([event["status"] for event in events], ["started", "returned"])
        self.assertNotIn("usage", events[0])


if __name__ == "__main__":
    unittest.main()
