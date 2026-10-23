"""Successor contracts: both selectors share all regressions and closure pins."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
ENTRY = SCRIPTS / "acquire_longform_sections_v3.py"


class SuccessorProvenanceTests(unittest.TestCase):
    def engine(self):
        self.assertTrue(ENTRY.is_file(), "versioned successor entry must exist")
        return importlib.import_module("acquire_longform_core_v3")

    def contract(self, engine, base, policy="video-only-v2"):
        url = base / "url.txt"
        url.write_text("https://www.youtube.com/watch?v=exact123\n", encoding="utf-8")
        identity = dict(video_id="exact123", title="fixture", uploader="fixture",
                        duration_seconds="2", availability="public", is_live=False)
        root = base / "contract"
        contract = engine.initialize_contract(url, root, identity, policy_name=policy)
        return root, contract

    def test_shared_selectors_and_complete_local_source_closure_are_bound(self):
        engine = self.engine()
        for name, selector in [("legacy-v1", "18/b[height<=360]"), ("video-only-v2", "bv[height<=360]")]:
            with self.subTest(policy=name), tempfile.TemporaryDirectory() as tmp:
                root, contract = self.contract(engine, Path(tmp), name)
                provenance = contract["pipeline_provenance"]
                self.assertEqual(provenance["policy_name"], name)
                self.assertEqual(contract["settings"]["source_format_selector"], selector)
                self.assertEqual(set(provenance["implementation_sha256"]), {
                    "acquire_longform_core_v3.py", "acquire_longform_policy_v3.py", "acquire_longform_sections_v3.py"})
                for file, digest in provenance["implementation_sha256"].items():
                    self.assertEqual(digest, hashlib.sha256((SCRIPTS / file).read_bytes()).hexdigest().upper())
                for command in [engine.build_ytdlp_full_command("yt-dlp", policy_name=name),
                    engine.build_ytdlp_section_command("yt-dlp", contract["sections"][0], policy_name=name)]:
                    self.assertEqual(command[command.index("--format") + 1], selector)
                engine._load_contract(root)

    def test_each_closure_member_tamper_is_rejected_before_and_after_contract_creation(self):
        engine = self.engine()
        for target in ("acquire_longform_core_v3.py", "acquire_longform_policy_v3.py", "acquire_longform_sections_v3.py"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                code = base / "code"
                code.mkdir()
                for name in engine.implementation_hashes():
                    shutil.copyfile(SCRIPTS / name, code / name)
                spec = importlib.util.spec_from_file_location("closure_fixture_core", code / "acquire_longform_core_v3.py")
                clone = importlib.util.module_from_spec(spec)
                saved_policy = sys.modules.pop("acquire_longform_policy_v3", None)
                sys.path.insert(0, str(code))
                try:
                    spec.loader.exec_module(clone)
                finally:
                    sys.path.remove(str(code))
                    sys.modules.pop("acquire_longform_policy_v3", None)
                    if saved_policy is not None:
                        sys.modules["acquire_longform_policy_v3"] = saved_policy
                root, _ = self.contract(clone, base)
                with (code / target).open("a", encoding="utf-8") as handle:
                    handle.write("\n# disposable tamper fixture\n")
                with self.assertRaisesRegex(RuntimeError, "implementation.*(changed|mismatch)"):
                    clone._load_contract(root)
                with self.assertRaisesRegex(RuntimeError, "implementation.*(changed|mismatch)"):
                    clone.initialize_contract(base / "url.txt", base / "new", dict(
                        video_id="exact123", title="fixture", uploader="fixture", duration_seconds="2", availability="public", is_live=False))
                self.assertFalse((base / "new").exists())

    def test_settings_and_policy_tamper_fail_closed(self):
        engine = self.engine()
        for change in ("selector", "policy", "closure"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root, contract = self.contract(engine, Path(tmp))
                if change == "selector":
                    contract["settings"]["source_format_selector"] = "best"
                elif change == "policy":
                    contract["pipeline_provenance"]["policy_name"] = "legacy-v1"
                else:
                    contract["pipeline_provenance"]["implementation_sha256"].pop("acquire_longform_core_v3.py")
                (root / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    engine._load_contract(root)
        with self.assertRaises(ValueError):
            engine.build_ytdlp_full_command("yt-dlp", policy_name="unreviewed")

    def test_default_acquisition_paths_use_contract_policy_before_external_io(self):
        engine = self.engine()
        class AcquisitionBoundaryReached(Exception):
            pass
        for name, selector in [("legacy-v1", "18/b[height<=360]"), ("video-only-v2", "bv[height<=360]")]:
            for full in (False, True):
                with self.subTest(policy=name, full=full), tempfile.TemporaryDirectory() as tmp:
                    root, _ = self.contract(engine, Path(tmp), name)
                    commands = []
                    def stop_before_io(command, *args, **kwargs):
                        commands.append(command)
                        raise AcquisitionBoundaryReached()
                    with patch.object(engine.shutil, "which", return_value="fixture-yt-dlp"), patch.object(engine, "_run_monitored_ytdlp", stop_before_io):
                        with self.assertRaises(AcquisitionBoundaryReached):
                            if full:
                                engine.acquire_full_source(root)
                            else:
                                engine.acquire_section(root, 0)
                    self.assertEqual(len(commands), 1)
                    self.assertEqual(commands[0][commands[0].index("--format") + 1], selector)
                    self.assertEqual(list((root / "segments").iterdir()), [])
                    self.assertEqual({item.name for item in root.iterdir()}, {"contract.json", "segments"})


def load_tests(loader, tests, pattern):
    if not ENTRY.exists():
        return tests
    engine = importlib.import_module("acquire_longform_core_v3")
    from longform_contract_cases import LongformPlanningCases, LongformSegmentCases, LongformCompletionCases
    for name, selector in [("legacy-v1", "18/b[height<=360]"), ("video-only-v2", "bv[height<=360]")]:
        api = SimpleNamespace(**{key: value for key, value in vars(engine).items() if callable(value)})
        api.initialize_contract = partial(engine.initialize_contract, policy_name=name)
        api.build_ytdlp_full_command = partial(engine.build_ytdlp_full_command, policy_name=name)
        api.build_ytdlp_section_command = partial(engine.build_ytdlp_section_command, policy_name=name)
        api.main = partial(engine.main, default_policy=name)
        for cases in (LongformPlanningCases, LongformSegmentCases, LongformCompletionCases):
            variant = type(cases.__name__ + "_" + name, (cases,), {
                "api": api, "selector": selector, "schema": "longform-source-segments-v3", "script_path": ENTRY})
            tests.addTests(loader.loadTestsFromTestCase(variant))
    return tests
