import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from swarm2.automatic_profiles import (
    ApplicationIdentity,
    ApplicationProfileRule,
    AutomaticProfileError,
    AutomaticProfileSettings,
    AutomaticProfileStore,
    MAX_APPLICATIONS,
    MAX_RULE_FILE_BYTES,
    MAX_RULES,
    SCHEMA,
    SCHEMA_VERSION,
    automatic_profile_settings_path,
    resolve_profile,
)
from swarm2.configuration import PresetStore


def rule(
    rule_id="game",
    name="Game",
    profile_slot=2,
    match_kind="executable_path",
    match_value="/opt/game/bin/game",
    enabled=True,
):
    return ApplicationProfileRule(
        rule_id=rule_id,
        name=name,
        enabled=enabled,
        profile_slot=profile_slot,
        match_kind=match_kind,
        match_value=match_value,
    )


def document(**changes):
    value = AutomaticProfileSettings(
        enabled=True,
        default_profile_slot=1,
        rules=(rule(),),
    ).to_dict()
    value.update(changes)
    return value


class AutomaticProfileModelTests(unittest.TestCase):
    def test_round_trip_is_strict_and_bundle_ids_are_canonical(self):
        value = document()
        value["rules"][0]["match"] = {
            "kind": "bundle_id",
            "value": "COM.Example.MouseGame",
        }
        settings = AutomaticProfileSettings.from_dict(value)
        self.assertEqual(settings.rules[0].match_value, "com.example.mousegame")
        self.assertEqual(
            AutomaticProfileSettings.from_dict(settings.to_dict()), settings
        )

    def test_schema_version_and_exact_fields_are_required(self):
        cases = [
            {**document(), "schema": "other"},
            {**document(), "version": 2},
            {**document(), "unknown": True},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings.from_dict(value)

    def test_slots_and_booleans_reject_bool_integer_aliases(self):
        for key, bad in (("enabled", 1), ("default_profile_slot", True)):
            with self.subTest(key=key), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings.from_dict(document(**{key: bad}))
        for key, bad in (("enabled", 1), ("profile_slot", True)):
            value = document()
            value["rules"][0][key] = bad
            with self.subTest(rule_key=key), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings.from_dict(value)

    def test_invalid_rule_identifiers_names_and_match_objects_are_rejected(self):
        cases = []
        for key, bad in (("id", "bad id"), ("name", "\x00"), ("profile_slot", 6)):
            value = document()
            value["rules"][0][key] = bad
            cases.append(value)
        for match in (
            {"kind": "basename", "value": "game"},
            {"kind": "bundle_id", "value": "not-a-bundle-id"},
            {"kind": "executable_path", "value": "relative/game"},
            {"kind": "executable_path", "value": "/opt/../bin/game"},
            {"kind": "executable_path", "value": "/"},
        ):
            value = document()
            value["rules"][0]["match"] = match
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings.from_dict(value)

    def test_duplicate_ids_and_application_identities_are_rejected(self):
        duplicate_id = document()
        duplicate_id["rules"].append(
            rule(match_value="/opt/other").to_dict()
        )
        duplicate_identity = document()
        duplicate_identity["rules"].append(
            rule(rule_id="other", profile_slot=3).to_dict()
        )
        duplicate_bundle_case = document()
        duplicate_bundle_case["rules"] = [
            rule(
                match_kind="bundle_id",
                match_value="com.example.Game",
            ).to_dict(),
            rule(
                rule_id="other",
                match_kind="bundle_id",
                match_value="COM.EXAMPLE.GAME",
            ).to_dict(),
        ]
        for value in (duplicate_id, duplicate_identity, duplicate_bundle_case):
            with self.subTest(value=value), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings.from_dict(value)

    def test_rule_count_is_bounded(self):
        value = document()
        value["rules"] = [
            rule(
                rule_id=f"rule-{index}",
                match_value=f"/opt/application-{index}",
            ).to_dict()
            for index in range(MAX_RULES + 1)
        ]
        with self.assertRaises(AutomaticProfileError):
            AutomaticProfileSettings.from_dict(value)

    def test_direct_rule_collection_requires_typed_tuple(self):
        for rules in ([rule()], ({"not": "a rule"},)):
            with self.subTest(rules=rules), self.assertRaises(AutomaticProfileError):
                AutomaticProfileSettings(rules=rules).to_dict()


class AutomaticProfileMatcherTests(unittest.TestCase):
    def test_disabled_settings_make_no_selection(self):
        settings = AutomaticProfileSettings(
            enabled=False,
            default_profile_slot=5,
            rules=(rule(),),
        )
        self.assertIsNone(
            resolve_profile(settings, [ApplicationIdentity("/opt/game/bin/game")])
        )

    def test_exact_executable_path_match_selects_rule_profile(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=1,
            rules=(rule(profile_slot=4),),
        )
        selection = resolve_profile(
            settings,
            [ApplicationIdentity(executable_path="/opt/game/bin/game")],
        )
        self.assertEqual(selection.profile_slot, 4)
        self.assertEqual(selection.source, "application")
        self.assertEqual(selection.rule.rule_id, "game")
        self.assertEqual(
            selection.application.executable_path, "/opt/game/bin/game"
        )

    def test_same_basename_at_another_path_does_not_match(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=3,
            rules=(rule(),),
        )
        selection = resolve_profile(
            settings,
            [ApplicationIdentity(executable_path="/tmp/game")],
        )
        self.assertEqual((selection.profile_slot, selection.source), (3, "default"))
        self.assertIsNone(selection.rule)
        self.assertIsNone(selection.application)

    def test_bundle_identifier_match_is_case_canonical(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=1,
            rules=(
                rule(
                    match_kind="bundle_id",
                    match_value="COM.Example.Game",
                    profile_slot=5,
                ),
            ),
        )
        selection = resolve_profile(
            settings,
            [ApplicationIdentity(bundle_id="com.example.game")],
        )
        self.assertEqual(selection.profile_slot, 5)

    def test_rule_order_is_priority_and_application_order_is_irrelevant(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=1,
            rules=(
                rule("editor", "Editor", 2, match_value="/usr/bin/editor"),
                rule("game", "Game", 4, match_value="/opt/game/bin/game"),
            ),
        )
        applications = [
            ApplicationIdentity("/opt/game/bin/game"),
            ApplicationIdentity("/usr/bin/editor"),
        ]
        first = resolve_profile(settings, applications)
        second = resolve_profile(settings, reversed(applications))
        self.assertEqual(first, second)
        self.assertEqual(first.rule.rule_id, "editor")
        self.assertEqual(first.profile_slot, 2)

    def test_disabled_rule_is_skipped_and_no_match_restores_default(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=5,
            rules=(rule(enabled=False),),
        )
        selection = resolve_profile(
            settings,
            [ApplicationIdentity("/opt/game/bin/game")],
        )
        self.assertEqual((selection.profile_slot, selection.source), (5, "default"))

    def test_invalid_application_snapshots_are_rejected(self):
        settings = AutomaticProfileSettings(enabled=True)
        cases = (
            [ApplicationIdentity()],
            [ApplicationIdentity(executable_path="relative")],
            [object()],
            None,
        )
        for applications in cases:
            with self.subTest(applications=applications), self.assertRaises(
                AutomaticProfileError
            ):
                resolve_profile(settings, applications)

    def test_application_snapshot_count_is_bounded(self):
        settings = AutomaticProfileSettings(enabled=True)
        applications = [
            ApplicationIdentity(executable_path=f"/opt/application-{index}")
            for index in range(MAX_APPLICATIONS + 1)
        ]
        with self.assertRaises(AutomaticProfileError):
            resolve_profile(settings, applications)


class AutomaticProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "automatic-profiles" / "rules.json"
        self.store = AutomaticProfileStore(self.path)

    def test_missing_file_loads_disabled_defaults(self):
        self.assertEqual(self.store.load(), AutomaticProfileSettings())

    def test_save_is_atomic_deterministic_and_separate_from_presets(self):
        settings = AutomaticProfileSettings(
            enabled=True,
            default_profile_slot=3,
            rules=(rule(),),
        )
        self.assertEqual(self.store.save(settings), self.path)
        first = self.path.read_bytes()
        self.store.save(settings)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(self.store.load(), settings)
        self.assertEqual(PresetStore(self.root).list(), [])
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_load_rejects_duplicate_fields_invalid_numbers_and_oversize(self):
        self.path.parent.mkdir(parents=True)
        cases = (
            b'{"schema":"a","schema":"b"}',
            b'{"value":NaN}',
            b" " * (MAX_RULE_FILE_BYTES + 1),
        )
        for raw in cases:
            self.path.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(AutomaticProfileError):
                self.store.load()

    def test_load_rejects_non_regular_file(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs are unavailable")
        self.path.parent.mkdir(parents=True)
        os.mkfifo(self.path)
        with self.assertRaises(AutomaticProfileError):
            self.store.load()

    def test_default_path_uses_a_preset_safe_subdirectory(self):
        path = automatic_profile_settings_path()
        self.assertEqual(path.name, "rules.json")
        self.assertEqual(path.parent.name, "automatic-profiles")


if __name__ == "__main__":
    unittest.main()
