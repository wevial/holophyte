"""The poll routes' generated contract and the pinned answers it describes.

Run: python3 -m unittest discover -s tests -p 'test_serve_contract.py' -v
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import typing
import unittest
from pathlib import Path

from holophyte.serve.contract import (
    COMMAND,
    ROOT,
    ROUTES,
    SCHEMA_DIR,
    ZOD_FILE,
    Status,
    main,
)
from holophyte.serve.contract_emit import ContractError, json_schema

FIXTURES = ROOT / "tests" / "fixtures" / "serve"
ANSWERS = ("status", "attention", "host-status", "host-attention")


def walk(value, annotation, where):
    """Raise AssertionError naming the first path whose value breaks its type."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if typing.is_typeddict(annotation):
        walk_object(value, annotation, where)
    elif origin in (typing.Union, types.UnionType):
        walk_union(value, args, where)
    elif origin is typing.Literal:
        assert value in args, f"{where}: {value!r} not in {args}"
    elif origin is list:
        assert isinstance(value, list), f"{where}: not a list"
        for index, item in enumerate(value):
            walk(item, args[0], f"{where}.{index}")
    elif origin is dict:
        assert isinstance(value, dict), f"{where}: not an object"
        for key, item in value.items():
            walk(item, args[1], f"{where}.{key}")
    else:
        walk_scalar(value, annotation, where)


def walk_object(value, annotation, where):
    hints = typing.get_type_hints(annotation)
    assert isinstance(value, dict), f"{where}: not an object"
    for key in sorted(value.keys() - hints.keys()):
        raise AssertionError(f"{where}.{key}: undeclared")
    for key in sorted(annotation.__required_keys__ - value.keys()):
        raise AssertionError(f"{where}.{key}: missing")
    for key, item in value.items():
        walk(item, hints[key], f"{where}.{key}")


def walk_union(value, args, where):
    kinds = {typing.get_args(kind)[0]: arg for arg in args
             if typing.is_typeddict(arg) and typing.get_origin(
                 kind := typing.get_type_hints(arg).get("kind"))
             is typing.Literal}
    if kinds and isinstance(value, dict):
        assert value.get("kind") in kinds, f"{where}.kind: undeclared kind"
        return walk(value, kinds[value["kind"]], where)
    failures = []
    for arg in args:
        try:
            return walk(value, arg, where)
        except AssertionError as bad:
            failures.append(str(bad))
    raise AssertionError("; ".join(failures))


def walk_scalar(value, annotation, where):
    if annotation is type(None):
        assert value is None, f"{where}: not null"
    elif annotation is float:
        assert isinstance(value, (int, float)) and not isinstance(value, bool), \
            f"{where}: not a number"
    else:
        assert isinstance(value, annotation) and (
            annotation is bool or not isinstance(value, bool)), \
            f"{where}: not {annotation.__name__}"


class GeneratedFilesTests(unittest.TestCase):
    def generated(self, root):
        paths = [ZOD_FILE, *(SCHEMA_DIR / path.name
                             for path in sorted((root / SCHEMA_DIR).iterdir()))]
        return {path: (root / path).read_bytes() for path in paths}

    def test_committed_files_pass_the_check_and_stay_untouched(self):
        before = self.generated(ROOT)
        done = subprocess.run([sys.executable, "-m", "holophyte.serve.contract",
                               "--check"], cwd=ROOT, capture_output=True,
                              text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.generated(ROOT), before)

    def test_an_edited_zod_file_and_a_stale_schema_fail_naming_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / SCHEMA_DIR, root / SCHEMA_DIR)
            (root / ZOD_FILE).parent.mkdir(parents=True)
            lines = (ROOT / ZOD_FILE).read_text().splitlines(keepends=True)
            (root / ZOD_FILE).write_text("".join(lines[:5] + lines[6:]))
            (root / SCHEMA_DIR / "stale.schema.json").write_text("{}\n")
            before = self.generated(root)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(["--check"], root=root)
            self.assertEqual(self.generated(root), before)
        self.assertEqual(code, 1)
        self.assertIn(str(ZOD_FILE), err.getvalue())
        self.assertIn(str(SCHEMA_DIR / "stale.schema.json"), err.getvalue())
        self.assertIn(f"`{COMMAND}`", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 3)

    def test_regenerating_an_edited_copy_restores_it_and_drops_the_stale_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / SCHEMA_DIR, root / SCHEMA_DIR)
            (root / ZOD_FILE).parent.mkdir(parents=True)
            (root / ZOD_FILE).write_text("// edited\n")
            (root / SCHEMA_DIR / "stale.schema.json").write_text("{}\n")
            self.assertEqual(main([], root=root), 0)
            self.assertFalse((root / SCHEMA_DIR / "stale.schema.json").exists())
            self.assertEqual(self.generated(root), self.generated(ROOT))
            self.assertEqual(main(["--check"], root=root), 0)


class Annotated(typing.TypedDict):
    value: typing.Annotated[int, "metadata"]


class EmitterTests(unittest.TestCase):
    def test_an_annotation_with_no_mapping_stops_the_generator_naming_its_field(self):
        with self.assertRaisesRegex(ContractError, r"^Annotated\.value: "):
            json_schema(Annotated, "GET /annotated")


class PinnedAnswerTests(unittest.TestCase):
    def test_each_pinned_answer_walks_clean_against_its_route_type(self):
        for stem in ANSWERS:
            with self.subTest(answer=stem):
                body = json.loads((FIXTURES / f"{stem}.json").read_text())
                walk(body, ROUTES[stem][0], stem)

    def test_an_answer_key_its_type_lacks_fails_the_walk_naming_it(self):
        hints = typing.get_type_hints(Status, include_extras=True)
        narrower = typing.TypedDict("Status", {key: hint for key, hint
                                               in hints.items()
                                               if key != "queued"})
        body = json.loads((FIXTURES / "status.json").read_text())
        with self.assertRaisesRegex(AssertionError, r"^status\.queued: undeclared$"):
            walk(body, narrower, "status")


if __name__ == "__main__":
    unittest.main()
