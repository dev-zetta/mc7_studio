"""PipeWire audio-session tests use fixtures and never change host volume."""

from collections import deque
from decimal import Decimal
import json
import subprocess
import sys
import unittest

from swarm2.host_audio import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_CONTROL_OUTPUT_BYTES,
    MAX_GRAPH_OUTPUT_BYTES,
    AmbiguousAudioSessions,
    AudioCommandFailed,
    AudioCommandResult,
    AudioCommandTimedOut,
    AudioPermissionDenied,
    AudioProviderUnavailable,
    AudioSessionChanged,
    AudioSessionInfo,
    AudioVolumeDispatch,
    AudioVolumeState,
    AudioVolumeWriteFailed,
    NoAudioSession,
    PipeWireAudioProvider,
    UnavailableAudioProvider,
    _CommandOutputLimit,
    _run_bounded_command,
    available_audio_sessions,
    create_host_audio_provider,
    is_audio_session_id,
    validate_audio_session_id,
)
from swarm2.media_volume_commands import MediaVolumeAction


MPV = "pipewire:application-id:mpv"
FIREFOX = "pipewire:application-id:org.mozilla.firefox"


def result(output=b"", *, code=0, errors=b""):
    return AudioCommandResult(code, output, errors)


def volume(value, *, muted=False):
    suffix = " [MUTED]" if muted else ""
    return result(f"Volume: {value}{suffix}\n".encode("ascii"))


def node(
    node_id,
    serial,
    *,
    application_id="mpv",
    process_binary="mpv",
    label="mpv",
    media_class="Stream/Output/Audio",
    object_type="PipeWire:Interface:Node",
):
    properties = {
        "media.class": media_class,
        "object.serial": serial,
        "application.name": label,
    }
    if application_id is not None:
        properties["application.id"] = application_id
    if process_binary is not None:
        properties["application.process.binary"] = process_binary
    return {
        "id": node_id,
        "type": object_type,
        "info": {"props": properties},
    }


def graph(*objects):
    return result(json.dumps(list(objects), separators=(",", ":")).encode())


class CommandFixture:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.calls = []

    def __call__(self, arguments, timeout, maximum):
        self.calls.append((tuple(arguments), timeout, maximum))
        if not self.responses:
            raise AssertionError("Unexpected host audio command")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def provider(responses=(), *, preferred_session=None, find_executable=None):
    runner = CommandFixture(responses)
    find = find_executable or (lambda name: "/usr/bin/" + name)
    return PipeWireAudioProvider(
        preferred_session=preferred_session,
        find_executable=find,
        runner=runner,
    ), runner


class PipeWireDiscoveryTests(unittest.TestCase):
    def test_discovery_returns_stable_ids_and_ignores_non_playback_objects(self):
        objects = (
            {"id": 1, "type": "PipeWire:Interface:Core", "info": {}},
            node(90, 1746),
            node(91, 1747, application_id="org.mozilla.firefox",
                 process_binary="firefox", label="Firefox"),
            node(92, 1748, application_id="ignored", label="Capture",
                 media_class="Stream/Input/Audio"),
        )
        instance, runner = provider([graph(*objects)])

        self.assertEqual(instance.available_sessions(), (
            AudioSessionInfo(MPV, "mpv"),
            AudioSessionInfo(FIREFOX, "Firefox"),
        ))
        self.assertEqual(runner.calls, [
            (("/usr/bin/pw-dump",), COMMAND_TIMEOUT_SECONDS,
             MAX_GRAPH_OUTPUT_BYTES),
        ])

    def test_process_binary_is_a_bounded_fallback_identity(self):
        instance, _ = provider([
            graph(node(90, 1746, application_id=None,
                       process_binary="legacy-player", label="Legacy"))
        ])
        self.assertEqual(instance.available_sessions(), (
            AudioSessionInfo(
                "pipewire:process-binary:legacy-player", "Legacy"),
        ))

    def test_duplicate_stable_identity_is_explicitly_ambiguous(self):
        instance, runner = provider([graph(node(90, 1), node(91, 2))])
        with self.assertRaisesRegex(AmbiguousAudioSessions, "same stable"):
            instance.available_sessions()
        self.assertEqual(len(runner.calls), 1)

    def test_unrelated_duplicate_does_not_block_unique_preferred_session(self):
        snapshot = graph(
            node(90, 1),
            node(91, 2, application_id="org.mozilla.firefox", label="Firefox"),
            node(92, 3, application_id="org.mozilla.firefox", label="Firefox"),
        )
        instance, runner = provider(
            [snapshot, volume("0.50")], preferred_session=MPV
        )

        self.assertEqual(
            instance.read_volume(), AudioVolumeState(50, "pipewire", MPV)
        )
        self.assertEqual(len(runner.calls), 2)

    def test_duplicate_preferred_session_is_explicitly_ambiguous(self):
        instance, runner = provider(
            [graph(node(90, 1), node(91, 2))], preferred_session=MPV
        )
        with self.assertRaisesRegex(AmbiguousAudioSessions, "selected application"):
            instance.read_volume()
        self.assertEqual(len(runner.calls), 1)

    def test_no_preference_requires_exactly_one_stream(self):
        instance, runner = provider([
            graph(
                node(90, 1),
                node(91, 2, application_id="org.mozilla.firefox",
                     process_binary="firefox", label="Firefox"),
            )
        ])
        with self.assertRaisesRegex(AmbiguousAudioSessions, "Choose"):
            instance.read_volume()
        self.assertEqual(len(runner.calls), 1)

        instance, _ = provider([graph()])
        with self.assertRaises(NoAudioSession):
            instance.read_volume()

    def test_preferred_session_must_still_be_active(self):
        instance, runner = provider(
            [graph(node(90, 1))], preferred_session=FIREFOX)
        with self.assertRaisesRegex(NoAudioSession, "selected application"):
            instance.read_volume()
        self.assertEqual(len(runner.calls), 1)

    def test_malformed_large_and_deep_graphs_fail_closed(self):
        deep = "[]"
        for _ in range(18):
            deep = "[" + deep + "]"
        invalid = (
            result(b"not-json"),
            result(b'{"duplicate":1,"duplicate":2}'),
            result(b"[NaN]"),
            result(deep.encode()),
            graph(node("90", 1)),
            graph(node(90, "1")),
            AudioCommandResult(0, b"[]" + b" " * MAX_GRAPH_OUTPUT_BYTES, b""),
        )
        for response in invalid:
            with self.subTest(length=len(response.stdout)):
                instance, _ = provider([response])
                with self.assertRaises(AudioCommandFailed):
                    instance.available_sessions()

    def test_bounded_labels_fall_back_and_unselectable_streams_fail_closed(self):
        instance, _ = provider([graph(node(90, 1, label="bad\nlabel"))])
        self.assertEqual(instance.available_sessions(), (
            AudioSessionInfo(MPV, "mpv"),
        ))

        for invalid in (
            node(91, 2, application_id=None, process_binary=None),
            node(91, 2, application_id="bad id", process_binary="also bad"),
        ):
            with self.subTest(invalid=invalid):
                instance, _ = provider([graph(invalid)])
                with self.assertRaisesRegex(AudioCommandFailed, "stable"):
                    instance.available_sessions()


class PipeWireVolumeTests(unittest.TestCase):
    def test_action_uses_absolute_fixed_argv_and_reports_verified_result(self):
        snapshot = graph(node(90, 1746))
        instance, runner = provider(
            [
                snapshot,
                volume("0.50"),
                snapshot,
                result(),
                snapshot,
                volume("0.52"),
            ],
            preferred_session=MPV,
        )

        dispatch = instance.perform(MediaVolumeAction.INCREASE)

        self.assertEqual(dispatch, AudioVolumeDispatch(
            MediaVolumeAction.INCREASE, 50, 52, "pipewire", MPV))
        self.assertEqual([call[0] for call in runner.calls], [
            ("/usr/bin/pw-dump",),
            ("/usr/bin/wpctl", "get-volume", "90"),
            ("/usr/bin/pw-dump",),
            ("/usr/bin/wpctl", "set-volume", "--limit", "1.0", "90", "52%"),
            ("/usr/bin/pw-dump",),
            ("/usr/bin/wpctl", "get-volume", "90"),
        ])
        self.assertEqual([call[2] for call in runner.calls], [
            MAX_GRAPH_OUTPUT_BYTES,
            MAX_CONTROL_OUTPUT_BYTES,
            MAX_GRAPH_OUTPUT_BYTES,
            MAX_CONTROL_OUTPUT_BYTES,
            MAX_GRAPH_OUTPUT_BYTES,
            MAX_CONTROL_OUTPUT_BYTES,
        ])
        self.assertEqual(len(runner.responses), 0)

    def test_source_exact_clamp_and_set_one_are_applied(self):
        for action, before, expected in (
            (MediaVolumeAction.DECREASE, "0.01", "0.00"),
            (MediaVolumeAction.INCREASE, "1.00", "1.00"),
            (MediaVolumeAction.SET_ONE, "0.74", "0.01"),
        ):
            with self.subTest(action=action):
                snapshot = graph(node(90, 1746))
                instance, runner = provider([
                    snapshot,
                    volume(before),
                    snapshot,
                    result(),
                    snapshot,
                    volume(expected),
                ])
                dispatch = instance.perform(action)
                target = int(Decimal(expected) * 100)
                self.assertEqual(dispatch.volume, target)
                self.assertEqual(runner.calls[-3][0][-1], f"{target}%")

    def test_muted_volume_is_read_without_changing_mute_state(self):
        instance, runner = provider([
            graph(node(90, 1746)), volume("0.37", muted=True)
        ])
        self.assertEqual(instance.read_volume(), AudioVolumeState(
            37, "pipewire", MPV))
        self.assertEqual(len(runner.calls), 2)

    def test_revalidation_blocks_id_or_serial_reuse_before_write(self):
        changes = (
            node(91, 1746),
            node(90, 9999),
        )
        for changed in changes:
            with self.subTest(changed=changed):
                instance, runner = provider([
                    graph(node(90, 1746)), volume("0.50"), graph(changed)
                ])
                with self.assertRaises(AudioSessionChanged):
                    instance.perform(MediaVolumeAction.INCREASE)
                self.assertEqual(len(runner.calls), 3)
                self.assertNotIn("set-volume", runner.calls[-1][0])

        instance, runner = provider([
            graph(node(90, 1746)), volume("0.50"), graph()
        ])
        with self.assertRaises(AudioSessionChanged):
            instance.perform(MediaVolumeAction.INCREASE)
        self.assertEqual(len(runner.calls), 3)

    def test_write_rejection_and_readback_mismatch_are_not_success(self):
        snapshot = graph(node(90, 1746))
        instance, _ = provider([
            snapshot, volume("0.50"), snapshot,
            result(code=3, errors=b"invalid volume"),
        ])
        with self.assertRaises(AudioVolumeWriteFailed):
            instance.perform(MediaVolumeAction.INCREASE)

        instance, _ = provider([
            snapshot,
            volume("0.50"),
            snapshot,
            result(),
            snapshot,
            volume("0.51"),
        ])
        with self.assertRaisesRegex(AudioVolumeWriteFailed, "did not match"):
            instance.perform(MediaVolumeAction.INCREASE)

        instance, _ = provider([
            snapshot,
            volume("0.50"),
            snapshot,
            result(),
            snapshot,
            result(b"garbage"),
        ])
        with self.assertRaisesRegex(AudioVolumeWriteFailed, "verified"):
            instance.perform(MediaVolumeAction.INCREASE)

        for malformed in (object(), _CommandOutputLimit()):
            with self.subTest(malformed=type(malformed).__name__):
                instance, _ = provider([
                    snapshot, volume("0.50"), snapshot, malformed
                ])
                with self.assertRaises(AudioVolumeWriteFailed):
                    instance.perform(MediaVolumeAction.INCREASE)

    def test_post_write_identity_drift_cannot_verify_the_result(self):
        snapshot = graph(node(90, 1746))
        for changed in (node(91, 1746), node(90, 9999)):
            with self.subTest(changed=changed):
                instance, runner = provider([
                    snapshot,
                    volume("0.50"),
                    snapshot,
                    result(),
                    graph(changed),
                ])
                with self.assertRaisesRegex(
                    AudioVolumeWriteFailed, "could not be verified"
                ):
                    instance.perform(MediaVolumeAction.INCREASE)
                self.assertEqual(runner.calls[-2][0][1], "set-volume")
                self.assertEqual(runner.calls[-1][0], ("/usr/bin/pw-dump",))

        instance, runner = provider([
            snapshot,
            volume("0.50"),
            snapshot,
            result(),
            graph(node(90, 1746), node(91, 1747)),
        ])
        with self.assertRaisesRegex(
            AudioVolumeWriteFailed, "could not be verified"
        ):
            instance.perform(MediaVolumeAction.INCREASE)
        self.assertEqual(runner.calls[-2][0][1], "set-volume")
        self.assertEqual(runner.calls[-1][0], ("/usr/bin/pw-dump",))

        for response, error in (
            (result(code=1, errors=b"Permission denied"),
             AudioPermissionDenied),
            (result(code=1, errors=b"failed to connect"),
             AudioProviderUnavailable),
            (subprocess.TimeoutExpired(("wpctl",), 1),
             AudioCommandTimedOut),
        ):
            with self.subTest(error=error.__name__):
                instance, _ = provider([
                    snapshot, volume("0.50"), snapshot, response
                ])
                with self.assertRaises(error):
                    instance.perform(MediaVolumeAction.INCREASE)

    def test_invalid_or_boosted_volume_never_reaches_set_volume(self):
        responses = (
            result(b"Volume: nope\n"),
            volume("1.25"),
            volume("0.505"),
            result(b"Volume: 0.50 trailing\n"),
            result(b"\xff"),
        )
        for response in responses:
            with self.subTest(output=response.stdout):
                instance, runner = provider([graph(node(90, 1)), response])
                with self.assertRaises(AudioCommandFailed):
                    instance.perform(MediaVolumeAction.INCREASE)
                self.assertEqual(len(runner.calls), 2)

    def test_invalid_action_runs_no_commands(self):
        instance, runner = provider([])
        with self.assertRaises(TypeError):
            instance.perform("increase")
        self.assertEqual(runner.calls, [])


class HostAudioBoundaryTests(unittest.TestCase):
    def test_factory_is_linux_only_and_does_not_run_commands(self):
        runner = CommandFixture()
        finds = []

        def find(name):
            finds.append(name)
            return "/usr/bin/" + name

        linux = create_host_audio_provider(
            system="Linux", find_executable=find, runner=runner)
        darwin = create_host_audio_provider(
            system="Darwin", find_executable=find, runner=runner)
        windows = create_host_audio_provider(
            system="Windows", find_executable=find, runner=runner)

        self.assertIsInstance(linux, PipeWireAudioProvider)
        self.assertIsInstance(darwin, UnavailableAudioProvider)
        self.assertIsInstance(windows, UnavailableAudioProvider)
        self.assertEqual(finds, ["pw-dump", "wpctl"])
        self.assertEqual(runner.calls, [])
        for unavailable in (darwin, windows):
            with self.assertRaises(AudioProviderUnavailable):
                unavailable.available_sessions()

    def test_missing_tools_and_command_error_classes_are_explicit(self):
        instance, runner = provider([], find_executable=lambda _name: None)
        with self.assertRaises(AudioProviderUnavailable):
            instance.available_sessions()
        self.assertEqual(runner.calls, [])

        for response, error in (
            (result(code=1, errors=b"Permission denied"), AudioPermissionDenied),
            (result(code=1, errors=b"failed to connect"), AudioProviderUnavailable),
            (subprocess.TimeoutExpired(("pw-dump",), 1), AudioCommandTimedOut),
            (_CommandOutputLimit(), AudioCommandFailed),
            (object(), AudioCommandFailed),
        ):
            with self.subTest(error=error.__name__):
                instance, _ = provider([response])
                with self.assertRaises(error):
                    instance.available_sessions()

    def test_public_discovery_helper_uses_provider_without_usb_or_writes(self):
        runner = CommandFixture([graph(node(90, 1))])
        self.assertEqual(available_audio_sessions(
            system="Linux",
            find_executable=lambda name: "/usr/bin/" + name,
            runner=runner,
        ), (AudioSessionInfo(MPV, "mpv"),))
        self.assertEqual([call[0] for call in runner.calls], [
            ("/usr/bin/pw-dump",),
        ])

    def test_selector_and_models_reject_unbounded_or_wrong_types(self):
        self.assertEqual(validate_audio_session_id(MPV), MPV)
        self.assertTrue(is_audio_session_id(MPV))
        for invalid in (
            None,
            "",
            "mpv",
            "pipewire:application-id:bad id",
            "pipewire:node-id:90",
            "pipewire:application-id:" + "x" * 129,
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(is_audio_session_id(invalid))
                with self.assertRaises(ValueError):
                    validate_audio_session_id(invalid)

        with self.assertRaises(TypeError):
            AudioVolumeState(True, "pipewire", MPV)
        with self.assertRaises(TypeError):
            AudioSessionInfo(MPV, "bad\nlabel")

    def test_real_runner_enforces_output_and_time_limits(self):
        with self.assertRaises(_CommandOutputLimit):
            _run_bounded_command(
                (sys.executable, "-c", "import sys;sys.stdout.write('x'*257)"),
                2,
                256,
            )
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded_command(
                (sys.executable, "-c", "import time;time.sleep(2)"),
                0.05,
                256,
            )


if __name__ == "__main__":
    unittest.main()
