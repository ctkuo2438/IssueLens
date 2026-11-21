"""The tool registry and the single declaration of the tools' checkpointed state."""

import ast
from dataclasses import fields
import json
from pathlib import Path
import unittest

import httpx

from backend.app.github import TOOL_REGISTRY, TOOL_SPECS, TOOLS, GitHubToolState, GitHubTools, ToolError
from test_mvp import TARGET


GITHUB = Path(__file__).resolve().parents[1] / "backend" / "app" / "github.py"
# Configuration GitHubTools keeps outside its checkpointed state, set once in __init__.
CONFIGURATION = {"client", "target", "clip", "require_public", "max_candidates", "max_candidate_reads"}


def offline_tools() -> GitHubTools:
    # These checks make no request, so the client never opens a connection.
    return GitHubTools(httpx.AsyncClient(), TARGET, lambda text, limit: (text, False))


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_each_tool_is_declared_once_and_the_model_sees_them_in_order(self):
        self.assertEqual(len(TOOL_REGISTRY), len(TOOL_SPECS))
        self.assertEqual([tool["name"] for tool in TOOLS], [spec.name for spec in TOOL_SPECS])

    def test_cache_keys_keep_the_format_saved_in_checkpoints(self):
        # Checkpoints store these keys, so a resumed run must build the same ones.
        cases = [
            ("get_issue", "{}", ["get_issue", 0]),
            ("get_issue_comments", '{"page":2}', ["get_issue_comments", 2]),
            ("search_issues", '{"query":"CSV  export"}', ["search_issues", "CSV export"]),
            ("get_related_issue", '{"issue_number":41}', ["get_related_issue", 41]),
            ("get_repo_readme", "{}", ["get_repo_readme", 0]),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(TOOL_REGISTRY[name].parse(arguments).key, json.dumps(expected))

    async def test_an_unknown_tool_is_refused_before_its_arguments_are_read(self):
        with self.assertRaisesRegex(ToolError, "not available"):
            await offline_tools().execute("delete_repository", "not JSON")


class StateDeclarationTests(unittest.TestCase):
    def test_a_checkpoint_is_a_copy_that_restores_the_same_state(self):
        tools = offline_tools()
        tools.limitations.append("A fixture limitation.")
        tools.comment_pages[1] = (["S2"], False)
        saved = tools.checkpoint()
        tools.limitations.append("Added after the checkpoint.")
        self.assertEqual(saved.limitations, ["A fixture limitation."])
        restored = offline_tools()
        restored.restore(saved)
        self.assertEqual(restored.checkpoint(), saved)

    def test_github_tools_keeps_state_only_in_the_declared_fields(self):
        # A new self.attribute outside GitHubToolState would be missing from checkpoints,
        # and a resumed run would silently start it over. Declare state as a field instead.
        declared = {item.name for item in fields(GitHubToolState)} | CONFIGURATION
        tree = ast.parse(GITHUB.read_text(), filename=str(GITHUB))
        tools_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GitHubTools"
        )
        assigned = set()
        for node in ast.walk(tools_class):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            assigned.update(
                item.attr for target in targets for item in ast.walk(target)
                if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name) and item.value.id == "self"
            )
        self.assertLessEqual(assigned, declared, "Declare new GitHubTools state in GitHubToolState.")
        self.assertTrue(CONFIGURATION <= assigned)
