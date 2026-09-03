"""Findings fingerprint + overlap for the v2 store (docs/v2/state-model.md §2).

The contract under test, in the doc's words: `findingsFingerprint` is a "hash
of sorted (path:line:severity) tuples", and it "is what makes stuck-review
detection mechanical: if the last `reviewStuckWindow` rounds share a
fingerprint (or overlap above a threshold), the review isn't converging".

So the properties asserted here are the ones a stuck-review check depends on:
emission order cannot change a fingerprint, the three key fields each can,
prose fields cannot, and overlap reports the shared fraction of both rounds'
findings. Nothing recomputes a digest — the two golden values below are pinned
literals, because a fingerprint is persisted in `reviewRounds` and compared
against rounds recorded by earlier releases. The multi-finding golden also
pins the digest across *processes*: a canonical form that leaked Python's
per-process set iteration order would hash a round differently on every run.

Pure functions, so no database: these tests take no store connection at all.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import unittest

import store

# Recorded from the shipped implementation, not recomputed: these pin the
# stored value itself, so a later change to the canonical encoding that
# silently invalidates every fingerprint already in a database fails here.
EMPTY_ROUND_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ROUND_DIGEST = "f60c148a90af030c1cf9c416108c2430fe2358ef6baadfdf477ead0bcd493e90"


def finding(path, severity, line=None, **extra):
    """One finding in §2's shape; `line`, `criterion` and `message` optional."""
    item = {"path": path, "severity": severity, "message": f"{severity} at {path}"}
    if line is not None:
        item["line"] = line
    item.update(extra)
    return item


ROUND = [
    finding("store.py", "p0", line=41, criterion="rejects illegal transitions"),
    finding("tests/test_store_claim.py", "nit"),
    finding("factory.py", "p2", line=7),
]


class FingerprintTests(unittest.TestCase):
    def test_order_does_not_change_the_fingerprint(self):
        """Two rounds raising the same findings in different orders match."""
        shuffled = [ROUND[2], ROUND[0], ROUND[1]]
        self.assertEqual(
            store.findings_fingerprint(ROUND),
            store.findings_fingerprint(shuffled),
        )

    def test_each_key_field_changes_the_fingerprint(self):
        """A different path, line or severity is a different round."""
        baseline = store.findings_fingerprint([finding("store.py", "p1", line=12)])
        for label, changed in (
            ("path", finding("factory.py", "p1", line=12)),
            ("line", finding("store.py", "p1", line=13)),
            ("severity", finding("store.py", "p2", line=12)),
            ("line dropped", finding("store.py", "p1")),
        ):
            with self.subTest(changed=label):
                self.assertNotEqual(baseline, store.findings_fingerprint([changed]))

    def test_prose_does_not_change_the_fingerprint(self):
        """A reworded message or criterion is the same complaint, so it matches.

        The point of the key: a reviewer that rephrases itself every round must
        not look like a reviewer that is finding new things.
        """
        reworded = [
            dict(item, message="rewritten in round two", criterion="something else")
            for item in ROUND
        ]
        self.assertEqual(
            store.findings_fingerprint(ROUND),
            store.findings_fingerprint(reworded),
        )

    def test_empty_round_returns_the_stable_sentinel(self):
        """Zero findings fingerprints to a fixed value, not an error."""
        self.assertEqual(store.findings_fingerprint([]), EMPTY_ROUND_DIGEST)
        self.assertEqual(store.EMPTY_FINGERPRINT, EMPTY_ROUND_DIGEST)
        self.assertNotEqual(store.findings_fingerprint(ROUND), EMPTY_ROUND_DIGEST)

    def test_fingerprint_is_stable_across_releases(self):
        """A known round still hashes to the value already stored for it."""
        self.assertEqual(store.findings_fingerprint(ROUND), ROUND_DIGEST)

    def test_malformed_findings_are_rejected(self):
        """A fingerprint over junk would compare fine and mean nothing."""
        for label, item in (
            ("no path", {"severity": "p0", "message": "m"}),
            ("no severity", {"path": "store.py", "message": "m"}),
            ("unknown severity", finding("store.py", "blocker")),
            ("non-integer line", finding("store.py", "p0", line="12")),
            ("zero line", finding("store.py", "p0", line=0)),
            ("negative line", finding("store.py", "p0", line=-2)),
            ("line at the absent sentinel", finding("store.py", "p0", line=-1)),
            ("field separator in path", finding("sto\x1fre.py", "p0", line=1)),
            ("record separator in path", finding("sto\x1ere.py", "p0", line=1)),
        ):
            with self.subTest(finding=label):
                with self.assertRaises(ValueError):
                    store.findings_fingerprint([item])

    def test_a_path_cannot_forge_a_record_boundary(self):
        """A path carrying the separators cannot smuggle in a second finding.

        The canonical form joins fields and records on ASCII separators and
        escapes nothing, so a path free to hold them could serialize to another
        round's bytes: a two-finding round and the one finding whose path spells
        out its separators hashed alike. That collision is the worst kind §6 can
        read — the two rounds below share *no* key, overlap 0.0, and would have
        matched fingerprints, which reads as "the same round twice" and trips a
        stuck review on a reviewer that changed its mind completely. Rejecting
        the separators is what keeps the encoding unambiguous without escaping,
        and so keeps the golden digests above valid.
        """
        honest = [finding("a", "p0", line=1), finding("b", "p1", line=2)]
        forged = [finding("a\x1f1\x1fp0\x1eb", "p1", line=2)]
        self.assertEqual(store.findings_overlap(honest, honest), 1.0)
        with self.assertRaises(ValueError):
            store.findings_fingerprint(forged)
        with self.assertRaises(ValueError):
            store.findings_overlap(honest, forged)

    def test_the_absent_line_sentinel_cannot_be_written_explicitly(self):
        """A finding cannot claim the key that "no line" normalizes to.

        Absent has to key at *some* value, and it keys at -1. If a reviewer
        could also cite line -1, that finding and a whole-file one would share
        a key: identical fingerprints and 1.0 overlap for two different
        complaints, which is exactly the false "stuck review" §6 must not see.
        Rejecting it is what keeps the sentinel private to this module.
        """
        with self.assertRaises(ValueError):
            store.findings_fingerprint([finding("store.py", "p0", line=store._NO_LINE)])
        with self.assertRaises(ValueError):
            store.findings_overlap(
                [finding("store.py", "p0")],
                [finding("store.py", "p0", line=store._NO_LINE)],
            )


class OverlapTests(unittest.TestCase):
    def test_two_of_three_shared(self):
        """Two rounds sharing 2 of 3 findings overlap on 2 of the 4 raised."""
        later = [
            ROUND[0],
            ROUND[1],
            finding("linear_provider.py", "p1", line=88),
        ]
        self.assertEqual(store.findings_overlap(ROUND, later), 0.5)

    def test_one_key_rounds_overlap_only_when_the_complaint_agrees(self):
        """Run 55: two one-finding rounds on the same file with no line
        keyed alike and scored 1.00 although they complained about different
        things. A single key is too coarse to read as 'the same round twice'
        on its own; the message has to agree as well."""
        first = finding("ticket_template.py", "p2", message=(
            "scans only unchecked criteria (`t.acceptance`). In a valid mixed"
            " checked/unchecked ticket, ignored paths and operator phrases in"
            " `t.acceptance_done` are missed"))
        second = finding("ticket_template.py", "p2", message=(
            "rejects dot-prefixed path tokens. For `.cache/report.html`"
            " ignored by `.cache/*`, Git reports it ignored, but"
            " `path_candidates()` returns nothing and validation passes"))
        self.assertEqual(store.findings_overlap([first], [second]), 0.0)

        rewrapped = dict(first, message="  " + first["message"].upper()
                         .replace(" ", "\n  "))
        self.assertEqual(store.findings_overlap([first], [rewrapped]), 1.0)
        self.assertEqual(
            store.findings_fingerprint([first]),
            store.findings_fingerprint([second]))

    def test_repeated_round_overlaps_completely(self):
        """The same findings twice is total overlap — §6's stuck review."""
        self.assertEqual(store.findings_overlap(ROUND, list(reversed(ROUND))), 1.0)

    def test_disjoint_rounds_do_not_overlap(self):
        """Nothing shared is 0.0, whatever the rounds' sizes."""
        later = [finding("README.md", "nit"), finding("AGENTS.md", "p2", line=4)]
        self.assertEqual(store.findings_overlap(ROUND, later), 0.0)

    def test_empty_rounds_overlap_completely(self):
        """Two rounds that both found nothing are the same round."""
        self.assertEqual(store.findings_overlap([], []), 1.0)

    def test_overlap_is_symmetric(self):
        """Which round is 'earlier' cannot change the measure."""
        later = [ROUND[0], finding("README.md", "nit")]
        self.assertEqual(
            store.findings_overlap(ROUND, later),
            store.findings_overlap(later, ROUND),
        )
