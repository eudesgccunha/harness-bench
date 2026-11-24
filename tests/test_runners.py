"""Unit tests for the runner protocol, the template adapter and the runner registry."""

import unittest

import fixtures  # noqa: F401  (puts the repo root on sys.path)
import harness_bench as hb
from hbench.runners import PARSERS, runner_for
from hbench.runners.base import HarnessRunner, TemplateRunner, build_argv


def cfg_for(parser: str, **extra: object) -> dict[str, object]:
    return {"command": ["agent", "--model", "{model}", "{prompt}"], "parser": parser, **extra}


class TemplateRunnerTests(unittest.TestCase):
    def test_command_matches_the_engine_build_cmd_for_provider_and_plain_models(self) -> None:
        template = ["{provider}", "{model_name}", "{model}", "{prompt}"]
        runner = TemplateRunner({"command": template, "parser": "claude"}, PARSERS["claude"])
        for model in ("p/m", "m"):
            self.assertEqual(runner.command(model, "go"), hb.build_cmd(template, model, "go"))

    def test_build_argv_leaves_unknown_braces_to_the_template_fields(self) -> None:
        self.assertEqual(build_argv(["[{provider}]", "{model_name}"], "m", "go"), ["[]", "m"])

    def test_version_defaults_to_unknown_and_reads_the_config(self) -> None:
        plain = TemplateRunner(cfg_for("claude"), PARSERS["claude"])
        pinned = TemplateRunner(cfg_for("claude", version="2.1"), PARSERS["claude"])
        self.assertEqual((plain.version(), pinned.version()), ("unknown", "2.1"))

    def test_name_is_the_parser_name_and_prepare_is_a_no_op(self) -> None:
        runner = TemplateRunner(cfg_for("pi"), PARSERS["pi"])
        runner.prepare(fixtures.ROOT)  # returns nothing and touches nothing
        self.assertEqual(runner.name, "pi")

    def test_parse_usage_delegates_to_the_parser(self) -> None:
        runner = TemplateRunner(cfg_for("null"), PARSERS["null"])
        usage = runner.parse_usage("anything")
        self.assertEqual(usage and usage["cost_reported"], 0.0)


class RunnerForTests(unittest.TestCase):
    def test_every_built_in_parser_has_a_runner(self) -> None:
        for name in ("claude", "opencode", "pi", "cursor", "codex", "null"):
            runner = runner_for(cfg_for(name))
            self.assertIsInstance(runner, HarnessRunner)
            self.assertEqual(runner.name, name)

    def test_parsers_registry_lists_the_built_ins(self) -> None:
        self.assertEqual(set(PARSERS), {"claude", "opencode", "pi", "cursor", "codex", "null"})

    def test_unknown_parser_raises_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            runner_for(cfg_for("nope"))

    def test_engine_reexports_the_parsers_under_the_same_names(self) -> None:
        self.assertIs(hb.parse_claude, PARSERS["claude"])
        self.assertIs(hb.PARSERS, PARSERS)


if __name__ == "__main__":
    unittest.main()
