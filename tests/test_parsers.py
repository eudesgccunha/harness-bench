"""Unit tests for the output parsers, cost helpers and formatting. In-process, no subprocesses."""

import json
import unittest
from typing import Any

import fixtures  # noqa: F401  (puts the repo root on sys.path)
import harness_bench as hb


def jl(*objs: Any) -> str:
    return "\n".join(json.dumps(o) for o in objs)


class NumTests(unittest.TestCase):
    def test_returns_first_numeric_value_among_keys(self) -> None:
        self.assertEqual(hb.num({"a": "x", "b": 7}, "a", "b"), 7)

    def test_booleans_are_not_numbers(self) -> None:
        self.assertEqual(hb.num({"a": True}, "a"), 0)

    def test_non_dict_gives_zero(self) -> None:
        self.assertEqual(hb.num(None, "a"), 0)

    def test_missing_keys_give_zero(self) -> None:
        self.assertEqual(hb.num({}, "a"), 0)


class JsonLinesTests(unittest.TestCase):
    def test_skips_non_json_and_malformed_lines(self) -> None:
        text = 'progress\n{"a": 1}\n{broken\n  {"b": 2}  \n'
        self.assertEqual(list(hb.json_lines(text)), [{"a": 1}, {"b": 2}])


class ExpandEnvTests(unittest.TestCase):
    def test_unset_variable_expands_to_empty_string(self) -> None:
        self.assertEqual(hb.expand_env("a-${HB_SURELY_UNSET_VAR}-b"), "a--b")


class ClaudeParserTests(unittest.TestCase):
    def test_sums_model_usage_including_helper_models(self) -> None:
        out = jl(
            {
                "type": "result",
                "num_turns": 2,
                "total_cost_usd": 0.5,
                "modelUsage": {"a": {"inputTokens": 1, "outputTokens": 2}, "b": {"inputTokens": 10}},
            }
        )
        u = hb.parse_claude(out)
        self.assertEqual((u["input"], u["output"], u["turns"], u["cost_reported"]), (11, 2, 2, 0.5))

    def test_falls_back_to_usage_without_model_usage(self) -> None:
        out = jl({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7}})
        u = hb.parse_claude(out)
        self.assertEqual((u["input"], u["output"], u["cache_read"]), (5, 6, 7))

    def test_missing_cost_stays_none_not_zero(self) -> None:
        self.assertIsNone(hb.parse_claude(jl({"type": "result", "usage": {}}))["cost_reported"])

    def test_parses_single_pretty_printed_object(self) -> None:
        out = json.dumps({"type": "result", "usage": {"input_tokens": 3}}, indent=2)
        self.assertEqual(hb.parse_claude(out)["input"], 3)

    def test_non_json_output_returns_none(self) -> None:
        self.assertIsNone(hb.parse_claude("not json"))

    def test_stream_json_uses_the_result_event_and_ignores_others(self) -> None:
        out = jl({"type": "assistant"}, {"type": "result", "usage": {"input_tokens": 4}})
        self.assertEqual(hb.parse_claude(out)["input"], 4)


class OpencodeParserTests(unittest.TestCase):
    def test_sums_step_finish_events_and_accepts_dash_spelling(self) -> None:
        out = jl(
            {"type": "step_finish", "part": {"cost": 0.1, "tokens": {"input": 1, "cache": {"read": 4, "write": 5}}}},
            {"type": "step-finish", "tokens": {"input": 2, "cacheRead": 6, "cacheWrite": 7}},
            {"type": "text"},
        )
        u = hb.parse_opencode(out)
        self.assertEqual((u["input"], u["cache_read"], u["cache_write"], u["turns"]), (3, 10, 12, 2))
        self.assertAlmostEqual(u["cost_reported"], 0.1)

    def test_no_step_finish_returns_none(self) -> None:
        self.assertIsNone(hb.parse_opencode(jl({"type": "text"})))


class PiParserTests(unittest.TestCase):
    def test_counts_only_assistant_message_end(self) -> None:
        usage = {"input": 10, "cost": {"total": 0.25}}
        out = jl(
            {"type": "message_end", "message": {"role": "assistant", "usage": usage}},
            {"type": "message_end", "message": {"role": "user", "usage": usage}},
            {"type": "message_update", "message": {"role": "assistant", "usage": usage}},
        )
        u = hb.parse_pi(out)
        self.assertEqual((u["input"], u["turns"], u["cost_reported"]), (10, 1, 0.25))

    def test_numeric_cost_is_accepted(self) -> None:
        out = jl({"type": "message_end", "message": {"role": "assistant", "usage": {"cost": 0.5}}})
        self.assertEqual(hb.parse_pi(out)["cost_reported"], 0.5)

    def test_absent_cost_is_none_not_zero(self) -> None:
        out = jl({"type": "message_end", "message": {"role": "assistant", "usage": {"input": 1}}})
        self.assertIsNone(hb.parse_pi(out)["cost_reported"])

    def test_no_assistant_messages_returns_none(self) -> None:
        self.assertIsNone(hb.parse_pi(jl({"type": "session"})))


class NullParserTests(unittest.TestCase):
    def test_usage_is_zero_and_the_cost_is_a_real_zero(self) -> None:
        self.assertEqual(hb.parse_null("anything"), {**hb.empty_usage(), "cost_reported": 0.0})


class CursorParserTests(unittest.TestCase):
    def test_result_without_usage_returns_none(self) -> None:
        self.assertIsNone(hb.parse_cursor(jl({"type": "result", "result": "ok"})))

    def test_no_result_returns_none(self) -> None:
        self.assertIsNone(hb.parse_cursor("plain text"))

    def test_events_before_the_result_are_ignored(self) -> None:
        out = jl({"type": "system"}, {"type": "result", "usage": {"inputTokens": 2}})
        self.assertEqual(hb.parse_cursor(out)["input"], 2)

    def test_usage_fields_and_cost_are_read_when_present(self) -> None:
        out = jl(
            {
                "type": "result",
                "usage": {"inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3, "cacheWriteTokens": 4},
                "total_cost_usd": 0.75,
            }
        )
        u = hb.parse_cursor(out)
        self.assertEqual((u["input"], u["output"], u["cache_read"], u["cache_write"]), (1, 2, 3, 4))
        self.assertEqual((u["cost_reported"], u["turns"]), (0.75, 1))

    def test_zero_cost_is_reported_as_none(self) -> None:
        out = jl({"type": "result", "usage": {"inputTokens": 1}})
        self.assertIsNone(hb.parse_cursor(out)["cost_reported"])


class CodexParserTests(unittest.TestCase):
    def test_uses_last_cumulative_usage_and_splits_cached_input(self) -> None:
        out = jl(
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}},
            {"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 100, "output_tokens": 9}},
        )
        u = hb.parse_codex(out)
        self.assertEqual((u["input"], u["cache_read"], u["output"], u["turns"]), (200, 100, 9, 2))

    def test_cached_larger_than_input_never_goes_negative(self) -> None:
        out = jl({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 5}})
        self.assertEqual(hb.parse_codex(out)["input"], 0)

    def test_no_completed_turn_returns_none(self) -> None:
        self.assertIsNone(hb.parse_codex(jl({"type": "thread.started"})))


class ComputedCostTests(unittest.TestCase):
    USAGE = {"input": 1_000_000, "output": 1_000_000, "reasoning": 0, "cache_read": 1_000_000, "cache_write": 0}

    def test_missing_price_gives_none(self) -> None:
        self.assertIsNone(hb.computed_cost(self.USAGE, None))

    def test_all_zero_price_gives_none_not_zero(self) -> None:
        self.assertIsNone(hb.computed_cost(self.USAGE, {"input": 0, "output": 0, "_note": "free?"}))

    def test_cache_read_defaults_to_input_price(self) -> None:
        self.assertAlmostEqual(hb.computed_cost(self.USAGE, {"input": 1, "output": 2}), 4.0)

    def test_explicit_cache_read_price_wins(self) -> None:
        self.assertAlmostEqual(hb.computed_cost(self.USAGE, {"input": 1, "output": 2, "cache_read": 0.5}), 3.5)


class RowTagsTests(unittest.TestCase):
    def test_list_is_returned_as_is(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": ["a", "b"]}), ["a", "b"])

    def test_json_string_is_decoded(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": '["a"]'}), ["a"])

    def test_comma_separated_string_is_split(self) -> None:
        self.assertEqual(hb.row_tags({"request_tags": "a, b"}), ["a", "b"])

    def test_missing_or_non_list_gives_empty(self) -> None:
        self.assertEqual((hb.row_tags({}), hb.row_tags({"request_tags": "7"})), ([], []))


if __name__ == "__main__":
    unittest.main()
