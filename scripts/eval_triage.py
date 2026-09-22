#!/usr/bin/env python3
"""Replay labelled mentions against the configured typed-question service."""

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from holophyte import pr, thread_mentions  # noqa: E402
from holophyte.redact import safe_print as print  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "tests/fixtures/mentions/labelled.jsonl"
    )
    parser.add_argument(
        "--config", type=Path, help="Target TOML containing [questions]"
    )
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    args = parser.parse_args(argv)
    if not 0 <= args.min_accuracy <= 1:
        parser.error("--min-accuracy must be in [0, 1]")
    config = tomllib.loads(args.config.read_text()) if args.config else {}
    counts, correct, misses = Counter(), Counter(), []
    for line in args.fixture.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        label = row["label"]
        if label not in thread_mentions.MENTION_INTENT.criteria:
            parser.error(f"unknown fixture label: {label}")
        comments = [pr.Comment("", text) for text in row.get("earlier_comments", [])]
        comments.append(pr.Comment("", row["comment"]))
        thread = pr.Thread(
            "",
            row.get("file", ""),
            row.get("line"),
            "",
            comments[0].body,
            "",
            replies=tuple(comments[1:]),
        )
        result = thread_mentions.triage(thread, row.get("ticket_title", ""), config)
        counts[label] += 1
        if result["decision"] == label:
            correct[label] += 1
        else:
            misses.append((row, result))
    total = sum(counts.values())
    for label in thread_mentions.MENTION_INTENT.criteria:
        print(f"{label}: {correct[label]}/{counts[label]} correct")
    accuracy = sum(correct.values()) / total if total else 0
    print(f"Overall accuracy: {accuracy:.1%} ({sum(correct.values())}/{total})")
    for row, result in misses:
        print(
            f"MISS expected={row['label']} got={result['decision']} "
            f"reason={result['reason']}: {row['comment']}"
        )
    return int(not total or accuracy < args.min_accuracy)


if __name__ == "__main__":
    raise SystemExit(main())
