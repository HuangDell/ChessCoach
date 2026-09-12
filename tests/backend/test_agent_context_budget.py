from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from server.core.agent.context_budget import (
    ContextBudget, ContextBudgetExceeded, RunContextWindow, complete_turn_ends,
    estimate_input, measure_input,
)
from server.core.agent.summary import ConversationSummaryBuilder


def turns(count: int, size: int = 3000) -> list[dict]:
    return [item for i in range(count) for item in (
        {"role": "developer", "content": f"position {i}"},
        {"role": "user", "content": f"goal {i}: " + "x" * size},
        {"role": "assistant", "content": f"conclusion {i}"},
    )]


class ContextBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.budget = ContextBudget(capacity=65_536, max_output_tokens=1024, summary_max_output_tokens=1024)
        self.generate = AsyncMock(return_value="Preserve the learning goal and unresolved question.")
        self.builder = ConversationSummaryBuilder(self.generate, input_limit=60_000)

    async def test_under_budget_keeps_more_than_twelve_items_and_prefix(self):
        history = turns(6, 50)
        window = RunContextWindow(budget=self.budget, history=history)
        items = history + [{"role": "user", "content": "next?"}]
        self.assertEqual(items, await window.prepare({}, items, self.builder))
        self.generate.assert_not_awaited()
        self.assertEqual(0, window.cut)

    async def test_threshold_compacts_and_retains_four_complete_turns(self):
        history = turns(12)
        window = RunContextWindow(budget=self.budget, history=history)
        current = [{"role": "developer", "content": "current facts"}, {"role": "user", "content": "next?"}]
        result = await window.prepare({}, history + current, self.builder)
        self.assertEqual(24, window.cut)
        self.assertEqual(history[24:] + current, result[1:])
        self.assertIn("CONTINUITY", result[0]["content"])
        self.assertLessEqual(window.metrics["context_after_tokens"], self.budget.target)
        self.assertEqual(1, self.generate.await_count)
        self.assertEqual(36, len(history))

    async def test_two_compactions_merge_only_newly_covered_turns(self):
        history = turns(12)
        first = RunContextWindow(budget=self.budget, history=history)
        await first.prepare({}, history + [{"role": "user", "content": "first"}], self.builder)
        remaining = history[first.cut:] + turns(10)
        second = RunContextWindow(budget=self.budget, history=remaining, summary=first.summary,
                                  covered_items=first.cut, summary_version=1)
        await second.prepare({}, remaining + [{"role": "user", "content": "second"}], self.builder)
        payload = json.loads(self.generate.call_args.args[0])
        self.assertEqual(first.summary, payload["previous_summary"])
        self.assertEqual(remaining[0], payload["history"][0])
        self.assertGreater(second.covered_items + second.cut, first.cut)

    async def test_large_current_turn_is_never_summarized_or_sent(self):
        history = turns(6, 20)
        window = RunContextWindow(budget=self.budget, history=history)
        with self.assertRaises(ContextBudgetExceeded):
            await window.prepare({}, history + [{"role": "user", "content": "z" * 70_000}], self.builder)
        for call in self.generate.await_args_list:
            self.assertNotIn("z" * 100, call.args[0])
        self.assertEqual(0, window.cut)

    async def test_failure_preserves_old_window_if_safe_and_blocks_if_unsafe(self):
        self.generate.side_effect = ValueError("provider incomplete")
        history = turns(12)
        window = RunContextWindow(budget=self.budget, history=history, summary="old", summary_version=1)
        result = await window.prepare({}, history, self.builder)
        self.assertEqual(history, result[1:])
        self.assertEqual("old", window.summary)
        self.assertEqual(0, window.cut)
        with self.assertRaises(ContextBudgetExceeded):
            await window.prepare({}, history + [{"role": "user", "content": "x" * 70_000}], self.builder)
        self.assertEqual(1, self.generate.await_count)

    async def test_cancellation_does_not_advance_boundary(self):
        self.generate.side_effect = asyncio.CancelledError()
        window = RunContextWindow(budget=self.budget, history=turns(12))
        with self.assertRaises(asyncio.CancelledError):
            await window.prepare({}, window.history, self.builder)
        self.assertEqual(0, window.cut)
        self.assertEqual("", window.summary)

    def test_usage_is_per_request_and_cache_does_not_reduce_capacity(self):
        items = turns(2, 30)
        window = RunContextWindow(budget=self.budget, history=items)
        window.observe({}, items, 8383)
        window.observe({}, items, 8655)
        window.observe({}, items, 10167)
        self.assertEqual(10167, window.measurement.input_tokens)
        self.assertEqual(10167, window.metrics["context_peak_input_tokens"])
        self.assertLess(estimate_input({}, items, window.measurement), 12000)

    def test_changed_prefix_schema_and_model_invalidate_measurement(self):
        items = turns(2, 100)
        fixed = {"model": "a", "schema": {"type": "object"}}
        measured = measure_input(fixed, items, 10)
        self.assertLess(estimate_input(fixed, items, measured), estimate_input(fixed, items, None))
        changed = {**fixed, "model": "b"}
        self.assertEqual(estimate_input(changed, items, None), estimate_input(changed, items, measured))
        self.assertEqual(estimate_input(fixed, items[1:], None), estimate_input(fixed, items[1:], measured))

    def test_tool_boundaries_do_not_orphan_outputs(self):
        items = [
            {"role": "user", "content": "compare"},
            {"type": "function_call", "call_id": "a"},
            {"type": "function_call", "call_id": "b"},
            {"type": "function_call_output", "call_id": "a"},
            {"type": "function_call_output", "call_id": "b"},
            {"role": "assistant", "content": "answer"},
        ]
        self.assertEqual([6], complete_turn_ends(items))
        self.assertEqual([], complete_turn_ends(items[:4]))
        self.assertEqual([], complete_turn_ends(items[3:]))

    async def test_large_tool_result_triggers_check_inside_run(self):
        history = turns(10)
        window = RunContextWindow(budget=self.budget, history=history)
        initial = history + [{"role": "user", "content": "compare"}]
        await window.prepare({}, initial, self.builder)
        window.observe({}, initial, 30_000)
        growth = [{"type": "function_call", "call_id": "a"},
                  {"type": "function_call_output", "call_id": "a", "output": "x" * 10_000}]
        result = await window.prepare({}, initial + growth, self.builder)
        self.assertGreater(window.cut, 0)
        self.assertEqual(growth, result[-2:])

    async def test_legacy_summary_rebuilt_from_raw_history(self):
        window = RunContextWindow(budget=self.budget, history=turns(12), summary="legacy truncation")
        await window.prepare({}, window.history, self.builder)
        self.assertEqual("", json.loads(self.generate.call_args.args[0])["previous_summary"])
        self.assertEqual(1, window.summary_version)

    async def test_summary_chunks_full_turns_and_preserves_previous_summary(self):
        builder = ConversationSummaryBuilder(self.generate, input_limit=9000)
        history = turns(8)
        await builder.summarize(history, "old goal")
        self.assertGreater(self.generate.await_count, 1)
        payloads = [json.loads(call.args[0]) for call in self.generate.await_args_list]
        self.assertEqual(history, [item for payload in payloads for item in payload["history"]])
        self.assertEqual("old goal", payloads[0]["previous_summary"])
        self.assertEqual(self.generate.return_value, payloads[1]["previous_summary"])

    async def test_empty_summary_and_single_oversized_turn_fail(self):
        self.generate.return_value = " "
        with self.assertRaises(ValueError):
            await self.builder.summarize(turns(1))
        self.generate.reset_mock()
        with self.assertRaises(ValueError):
            await self.builder.summarize(turns(1, 70_000))
        self.generate.assert_not_awaited()

    def test_invalid_configuration_fails(self):
        for kwargs in ({"capacity": 30000}, {"target_ratio": .95}, {"max_output_tokens": 0}, {"max_output_tokens": 40000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ContextBudget(**kwargs)
