"""Fallback pull request descriptions."""
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.loop
import holophyte.pullrequest
from holophyte import pr


class MergePayloadTests(unittest.TestCase):
    def test_merge_commit_metadata(self):
        pull = pr.PullRequest("github.com", "example", "repo", 7,
                              "https://github.com/example/repo/pull/7")
        for method in ("squash", "merge", "rebase"):
            for body, message in (
                    ("## Summary\n\nKeep [links](https://example.com)\n"
                     "and details.\n\nAnother paragraph.\n\n"
                     "## Tests\nPassed.",
                     "Keep [links](https://example.com)\nand details."),
                    ("## Tests\nPassed.", ""),
                    ("## Summary\n\n## Tests\nPassed.", ""),
                    (None, "")):
                target = SimpleNamespace(config=lambda: {
                    "merge": {"pr_merge_method": method}})
                with self.subTest(method=method, body=body), patch.object(
                        pr, "rest", side_effect=lambda _t, _p, verb, path,
                        *args: {"title": "feat(x): do y (KO-1)", "body": body}
                        if verb == "GET" else {"merged": True, "sha": "landed"}
                        ) as rest:
                    self.assertEqual(pr.merge_pull_request(target, pull, "head"),
                                     "landed")
                expected = {"merge_method": method, "sha": "head"}
                if method != "rebase":
                    expected.update(commit_title="feat(x): do y (KO-1) (#7)",
                                    commit_message=message)
                    self.assertEqual(rest.call_args_list[0].args,
                                     (target, pull, "GET",
                                      "repos/example/repo/pulls/7"))
                self.assertEqual(rest.call_count, 1 if method == "rebase" else 2)
                self.assertEqual(rest.call_args.args,
                                 (target, pull, "PUT",
                                  "repos/example/repo/pulls/7/merge", expected))


class PrBodyStubTests(unittest.TestCase):
    def test_stub_uses_only_the_first_summary_paragraph(self):
        body = pr.pr_body_stub(
            {"id": "KO-441", "body": "# Contract\n\n## Summary\n\n"
             "Describe the change\non two lines.\n  \nSecond paragraph.\n\n"
             "## Acceptance criteria\n\nMust pass."},
            "the turn ran out of time", "https://linear.app/example/KO-441")
        self.assertEqual(
            body, "Describe the change on two lines.\n\n"
            "The description could not be written: the turn ran out of time"
            "\n\nLinear: KO-441 (https://linear.app/example/KO-441)")

    def test_stub_caps_summary_and_handles_a_missing_summary(self):
        body = pr.pr_body_stub(
            {"id": "KO-441", "body": "## Summary\n" + "Long text. " * 80},
            "empty reply", None)
        self.assertEqual(len(body.split("\n\n")[0]), 600)
        self.assertNotIn("## Summary", body)
        self.assertEqual(
            pr.pr_body_stub({"id": "KO-441"}, "empty reply", None),
            "The description could not be written: empty reply\n\nLinear: KO-441")

    def test_a_timeout_or_empty_body_uses_the_stub(self):
        target = SimpleNamespace(config=lambda: {})
        for reply, timed_out, reason in (
                ("TITLE: A title\n\nPartial text", True, "ran out of time"),
                ("TITLE: A title\n", False, "empty body")):
            with self.subTest(timed_out=timed_out), patch(
                    "holophyte.pullrequest.sh", return_value=""), patch(
                    "holophyte.babysitter.conventions", return_value=[]), patch.object(
                    holophyte.loop, "_timed", return_value=(reply, timed_out)):
                title, body = holophyte.pullrequest._written_pr_text(
                    target, None, None, "KO-131", "add a thing", "task/ko-131",
                    "## Summary\nThe thing, added.", 60, Path("/unused"),
                    monotonic(), 5, None)
            self.assertEqual(title, "KO-131: add a thing")
            self.assertTrue(body.startswith("The thing, added.\n\n"))
            self.assertIn("The description could not be written: ", body)
            self.assertIn(reason, body)
            self.assertTrue(body.endswith("Linear: KO-131"))
            self.assertNotIn("Partial text", body)
            self.assertNotIn("## Acceptance criteria", body)


class RequiredStatusContextTests(unittest.TestCase):
    def read_status(self, status, more=False):
        from holophyte import pr_status
        pull = pr.PullRequest("github.com", "example", "repo", 7,
                              "https://github.com/example/repo/pull/7")
        run = {"name": "vitest", "status": "completed", "conclusion": "success"}
        contexts = [{"__typename": "CheckRun", "name": "vitest",
                     "status": "COMPLETED", "conclusion": "SUCCESS"}]
        status_node = {"__typename": "StatusContext", "context": "Vercel",
                       "state": status}
        if status and not more:
            contexts.append(status_node)
        rollup = {"state": "SUCCESS", "contexts": {
            "nodes": contexts,
            "pageInfo": {"hasNextPage": more, "endCursor": "cursor"}}}
        node = {"headRefOid": "head", "commits": {"nodes": [
            {"commit": {"statusCheckRollup": rollup}}]}}
        responses = [{"repository": {"pullRequest": node}}]
        if more:
            responses.append({"repository": {"object": {"statusCheckRollup": {
                "contexts": {"nodes": [status_node],
                             "pageInfo": {"hasNextPage": False}}}}}})
        rules = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "Vercel"},
                                       {"context": "vitest"}]}}]
        with patch.object(pr_status, "graphql", side_effect=responses), \
                patch.object(pr_status, "rest", side_effect=[
                    {"total_count": 1, "check_runs": [run]}, rules, {}]), \
                patch.object(pr_status, "fold_checks",
                             wraps=pr_status.fold_checks) as fold:
            state = pr_status.pr_state(SimpleNamespace(), pull)
        return state, fold.call_args.args[1], pull

    def test_required_status_states_and_missing_context(self):
        for status, expected in (("SUCCESS", "success"), ("PENDING", "pending"),
                                 ("EXPECTED", "pending"), ("ERROR", "failure"),
                                 ("FAILURE", "failure"), (None, "pending")):
            with self.subTest(status=status):
                state, runs, _ = self.read_status(status)
                self.assertEqual(state.checks, expected)
                if status:
                    self.assertEqual([r["name"] for r in runs],
                                     ["vitest", "Vercel"])

    def test_pending_status_is_named_in_parked_reason(self):
        from holophyte import babysitter
        state, runs, pull = self.read_status("PENDING")
        self.assertIn({"name": "Vercel", "status": "pending",
                       "conclusion": "pending"}, runs)
        target = SimpleNamespace(config=lambda: {})
        with patch.object(babysitter, "monotonic", side_effect=[0, 999999]), \
                self.assertRaisesRegex(babysitter.WaitExpired,
                                       r"pending checks.*Vercel.*exceeded"):
            babysitter._settled_state(target, None, None, 1, pull, state)

    def test_status_context_on_later_rollup_page(self):
        state, runs, _ = self.read_status("SUCCESS", more=True)
        self.assertEqual(state.checks, "success")
        self.assertEqual([r["name"] for r in runs], ["vitest", "Vercel"])

    def test_status_context_pagination_rejects_repeated_cursor(self):
        from unittest.mock import Mock

        from holophyte.gates import InfraFailure
        from holophyte.pr_contexts import status_contexts_of

        rollup = {"contexts": {"nodes": [], "pageInfo": {
            "hasNextPage": True, "endCursor": "same-cursor"}}}
        node = {"headRefOid": "head", "commits": {"nodes": [
            {"commit": {"statusCheckRollup": rollup}}]}}
        graphql = Mock(side_effect=[
            {"repository": {"object": {"statusCheckRollup": rollup}}},
            AssertionError("Repeated cursor caused another request"),
        ])
        pull = SimpleNamespace(owner="example", name="repo")
        with self.assertRaisesRegex(InfraFailure, "repeated.*cursor"):
            status_contexts_of(SimpleNamespace(), pull, node, graphql)
        self.assertEqual(graphql.call_count, 1)
