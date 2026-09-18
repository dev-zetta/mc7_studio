"""Host media providers use fixtures only and never open an MC7 device."""

from collections import deque
import subprocess
import sys
import unittest
from unittest.mock import patch

from swarm2.host_media import (
    DISPATCH_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_BYTES,
    AmbiguousMediaPlayers,
    AppleMusicMediaProvider,
    MediaAction,
    MediaActionUnsupported,
    MediaCommandFailed,
    MediaCommandResult,
    MediaCommandTimedOut,
    MediaLoopStatus,
    MediaPlaybackState,
    MediaPlayerInfo,
    MediaPermissionDenied,
    MediaProviderUnavailable,
    MprisMediaProvider,
    NoMediaPlayer,
    UnavailableMediaProvider,
    _CommandOutputLimit,
    _run_bounded_command,
    available_media_players,
    create_host_media_provider,
)
from swarm2.media_player_ids import APPLE_MUSIC_PLAYER_ID


PLAYER = "org.mpris.MediaPlayer2.example"
OTHER = "org.mpris.MediaPlayer2.other"
PLAYER_OWNER = ":1.100"
OTHER_OWNER = ":1.101"


def result(output=b"()", *, code=0, errors=b""):
    return MediaCommandResult(code, output, errors)


def names(*values):
    rendered = repr(list(values)).encode("ascii")
    return result(b"(" + rendered + b",)\n")


def status(value):
    return result(f"(<'{value}'>,)\n".encode("ascii"))


def boolean(value):
    return result(b"(<true>,)\n" if value else b"(<false>,)\n")


def loop_status(value):
    return result(f"(<'{value}'>,)\n".encode("ascii"))


def owner(value):
    return result(f"({value!r},)\n".encode("ascii"))


class CommandFixture:
    def __init__(self, responses=(), *, owners=None):
        self.responses = deque(responses)
        self.calls = []
        self.owners = {} if owners is None else dict(owners)

    def __call__(self, arguments, timeout, maximum):
        self.calls.append((tuple(arguments), timeout, maximum))
        if "org.freedesktop.DBus.GetNameOwner" in arguments:
            response = self.owners.get(arguments[-1])
            if response is None:
                raise AssertionError("Unexpected MPRIS owner lookup")
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, MediaCommandResult):
                return response
            return owner(response)
        if not self.responses:
            raise AssertionError("Unexpected host command")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def mpris(responses, **changes):
    owners = changes.pop("owners", {
        PLAYER: PLAYER_OWNER,
        OTHER: OTHER_OWNER,
    })
    runner = CommandFixture(responses, owners=owners)
    values = {
        "find_executable": lambda name: "/usr/bin/gdbus",
        "runner": runner,
    }
    values.update(changes)
    return MprisMediaProvider(**values), runner


class MprisMediaProviderTests(unittest.TestCase):
    def test_discovery_lists_exact_players_without_status_or_action_calls(self):
        provider, runner = mpris([names(OTHER, PLAYER, PLAYER)])
        players = provider.available_players()

        self.assertEqual(players, (
            MediaPlayerInfo(PLAYER, "example", "mpris"),
            MediaPlayerInfo(OTHER, "other", "mpris"),
        ))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0][-1],
                         "org.freedesktop.DBus.ListNames")

    def test_play_pause_uses_one_playing_player_and_required_capabilities(self):
        provider, runner = mpris([
            names("org.freedesktop.DBus", PLAYER),
            status("Playing"),
            boolean(True),
            boolean(True),
            result(),
        ])
        dispatched = provider.perform(MediaAction.PLAY_PAUSE)

        self.assertEqual(
            (dispatched.action, dispatched.backend, dispatched.player_id,
             dispatched.prior_status),
            (MediaAction.PLAY_PAUSE, "mpris", PLAYER, "Playing"),
        )
        arguments = [call[0] for call in runner.calls]
        self.assertIn("org.freedesktop.DBus.ListNames", arguments[0])
        self.assertEqual(arguments[1][-2:], (
            "org.freedesktop.DBus.GetNameOwner", PLAYER))
        self.assertEqual(arguments[2][-2:], (
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus"))
        self.assertEqual(arguments[3][-2:], (
            "org.mpris.MediaPlayer2.Player", "CanControl"))
        self.assertEqual(arguments[4][-2:], (
            "org.mpris.MediaPlayer2.Player", "CanPause"))
        self.assertEqual(arguments[5][-1],
                         "org.mpris.MediaPlayer2.Player.PlayPause")
        self.assertEqual([
            call[call.index("--dest") + 1] for call in arguments[2:]
        ], [PLAYER_OWNER] * 4)
        self.assertTrue(all(call[0][:3] == ("/usr/bin/gdbus", "call", "--session")
                            for call in runner.calls))
        self.assertTrue(all(call[1:] == (0.75, MAX_COMMAND_OUTPUT_BYTES)
                            for call in runner.calls))

    def test_unique_playing_player_wins_over_an_idle_player(self):
        provider, runner = mpris([
            names(PLAYER, OTHER),
            status("Playing"),  # sorted bus name: example
            status("Paused"),
            boolean(True), boolean(True), result(),
        ])
        dispatched = provider.perform(MediaAction.NEXT)
        self.assertEqual(dispatched.player_id, PLAYER)
        self.assertEqual(runner.calls[-2][0][-1], "CanGoNext")
        self.assertEqual(runner.calls[-1][0][-1], "org.mpris.MediaPlayer2.Player.Next")

    def test_multiple_playing_or_idle_players_are_explicitly_ambiguous(self):
        cases = (("Playing", "Playing"), ("Paused", "Stopped"))
        for first, second in cases:
            with self.subTest(first=first, second=second):
                provider, runner = mpris([
                    names(PLAYER, OTHER), status(first), status(second)
                ])
                with self.assertRaises(AmbiguousMediaPlayers):
                    provider.perform(MediaAction.STOP)
                self.assertEqual(len(runner.calls), 5)

    def test_preferred_player_resolves_idle_ambiguity_without_probing_the_other(self):
        provider, runner = mpris([
            names(OTHER, PLAYER), status("Paused"),
            boolean(True), boolean(True), result(),
        ], preferred_player=OTHER)
        dispatched = provider.perform(MediaAction.PREVIOUS)
        self.assertEqual((dispatched.player_id, dispatched.prior_status),
                         (OTHER, "Paused"))
        queried_destinations = [
            call[0][call[0].index("--dest") + 1]
            for call in runner.calls[1:]
        ]
        self.assertEqual(
            queried_destinations,
            ["org.freedesktop.DBus"] + [OTHER_OWNER] * 4,
        )

    def test_missing_or_disappearing_players_do_not_dispatch(self):
        provider, runner = mpris([names("org.freedesktop.DBus")])
        with self.assertRaises(NoMediaPlayer):
            provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 1)

        provider, runner = mpris([
            names(PLAYER), result(code=1, errors=b"org.freedesktop.DBus.Error.NameHasNoOwner")
        ])
        with self.assertRaises(NoMediaPlayer):
            provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 3)

        provider, runner = mpris([
            names(PLAYER, OTHER),
            result(code=1, errors=b"org.freedesktop.DBus.Error.NameHasNoOwner"),
            status("Playing"), boolean(True), result(),
        ])
        with self.assertRaisesRegex(NoMediaPlayer, "closed during selection"):
            provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 3)

    def test_owner_exit_cannot_handoff_an_action_to_a_replacement(self):
        vanished = result(
            code=1,
            errors=b"org.freedesktop.DBus.Error.NameHasNoOwner")
        provider, runner = mpris([
            names(PLAYER), status("Playing"), boolean(True), boolean(True),
            vanished,
        ])
        with self.assertRaisesRegex(NoMediaPlayer, "closed"):
            provider.perform(MediaAction.NEXT)
        destinations = [
            call[0][call[0].index("--dest") + 1]
            for call in runner.calls[1:]
        ]
        self.assertEqual(
            destinations,
            ["org.freedesktop.DBus"] + [PLAYER_OWNER] * 4,
        )
        self.assertNotIn(PLAYER, destinations[1:])

    def test_owner_lookup_disappearance_and_malformed_output_fail_closed(self):
        vanished = result(
            code=1,
            errors=b"org.freedesktop.DBus.Error.NameHasNoOwner")
        provider, runner = mpris(
            [names(PLAYER)], owners={PLAYER: vanished})
        with self.assertRaisesRegex(NoMediaPlayer, "closed during selection"):
            provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 2)

        malformed = (
            result(b"('org.mpris.MediaPlayer2.replacement',)\n"),
            result(b"(':1',)\n"),
            result(b"(':1.2', 'extra')\n"),
            result(b"[':1.2']\n"),
            result(b"('\xff',)\n"),
        )
        for response in malformed:
            with self.subTest(response=response.stdout):
                provider, runner = mpris(
                    [names(PLAYER)], owners={PLAYER: response})
                with self.assertRaisesRegex(
                        MediaCommandFailed, "owner lookup"):
                    provider.perform(MediaAction.STOP)
                self.assertEqual(len(runner.calls), 2)

    def test_capability_false_stops_before_the_action_method(self):
        cases = (
            (MediaAction.STOP, "Playing", [boolean(False)]),
            (MediaAction.NEXT, "Playing", [boolean(True), boolean(False)]),
            (MediaAction.PREVIOUS, "Paused", [boolean(True), boolean(False)]),
            (MediaAction.PLAY_PAUSE, "Playing", [boolean(True), boolean(False)]),
            (MediaAction.PLAY_PAUSE, "Paused", [boolean(True), boolean(False)]),
        )
        for action, playback, capabilities in cases:
            with self.subTest(action=action, playback=playback):
                provider, runner = mpris([
                    names(PLAYER), status(playback), *capabilities
                ])
                with self.assertRaises(MediaActionUnsupported):
                    provider.perform(action)
                self.assertFalse(any(
                    call[0][-1].startswith("org.mpris.MediaPlayer2.Player.")
                    for call in runner.calls
                ))

    def test_play_pause_uses_can_play_when_not_currently_playing(self):
        for playback in ("Paused", "Stopped"):
            with self.subTest(playback=playback):
                provider, runner = mpris([
                    names(PLAYER), status(playback),
                    boolean(True), boolean(True), result(),
                ])
                provider.perform(MediaAction.PLAY_PAUSE)
                self.assertEqual(runner.calls[-2][0][-1], "CanPlay")

    def test_paused_play_pause_does_not_require_can_pause(self):
        provider, runner = mpris([
            names(PLAYER), status("Paused"),
            boolean(True),  # CanControl
            boolean(True),  # CanPlay; CanPause is deliberately not queried.
            result(),
        ])
        dispatched = provider.perform(MediaAction.PLAY_PAUSE)
        self.assertEqual(dispatched.prior_status, "Paused")
        queried_properties = [call[0][-1] for call in runner.calls
                              if call[0][-3].endswith("Properties.Get")]
        self.assertEqual(queried_properties,
                         ["PlaybackStatus", "CanControl", "CanPlay"])

    def test_invalid_action_fails_before_discovery(self):
        provider, runner = mpris([])
        for action in ("play_pause", None, 1):
            with self.subTest(action=action), self.assertRaises(TypeError):
                provider.perform(action)
        self.assertEqual(runner.calls, [])

    def test_malformed_discovery_properties_and_action_results_fail_closed(self):
        cases = (
            [result(b"not a list")],
            [names(PLAYER), status("Buffering")],
            [names(PLAYER), status("Playing"), result(b"(<1>,)")],
            [names(PLAYER), status("Playing"), boolean(True), result(b"(<1>,)")],
            [names(PLAYER), status("Playing"), boolean(True), boolean(True), result(b"unexpected")],
        )
        for responses in cases:
            with self.subTest(responses=responses):
                provider, _ = mpris(responses)
                with self.assertRaises(MediaCommandFailed):
                    provider.perform(MediaAction.PLAY_PAUSE)

    def test_permission_and_missing_executable_have_specific_failures(self):
        provider, _ = mpris([
            result(code=1, errors=b"GDBus.Error:org.freedesktop.DBus.Error.AccessDenied")
        ])
        with self.assertRaises(MediaPermissionDenied):
            provider.perform(MediaAction.STOP)

        runner = CommandFixture()
        provider = MprisMediaProvider(
            find_executable=lambda name: None, runner=runner)
        with self.assertRaises(MediaProviderUnavailable):
            provider.perform(MediaAction.STOP)
        self.assertEqual(runner.calls, [])

    def test_player_count_and_preferred_name_are_bounded(self):
        players = tuple(
            f"org.mpris.MediaPlayer2.player{chr(97 + index)}"
            for index in range(9)
        )
        provider, runner = mpris([names(*players)])
        with self.assertRaises(AmbiguousMediaPlayers):
            provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 1)
        for invalid in ("vlc", "org.mpris.MediaPlayer2.2bad", "", 4):
            with self.subTest(invalid=invalid), self.assertRaises((TypeError, ValueError)):
                MprisMediaProvider(preferred_player=invalid)

    def test_read_state_uses_mpris_playback_shuffle_and_loop_properties(self):
        provider, runner = mpris([
            names(PLAYER), status("Playing"), boolean(True),
            loop_status("Playlist"),
        ])
        self.assertEqual(provider.read_state(), MediaPlaybackState(
            True, True, MediaLoopStatus.PLAYLIST, "mpris", PLAYER))
        self.assertEqual(
            [call[0][-1] for call in runner.calls[2:]],
            ["PlaybackStatus", "Shuffle", "LoopStatus"],
        )
        self.assertEqual([
            call[0][call[0].index("--dest") + 1]
            for call in runner.calls[2:]
        ], [PLAYER_OWNER] * 3)

    def test_shuffle_toggles_the_read_write_mpris_property(self):
        for old, variant in ((False, "<true>"), (True, "<false>")):
            with self.subTest(old=old):
                provider, runner = mpris([
                    names(PLAYER), status("Paused"), boolean(True),
                    boolean(old), result(),
                ])
                dispatched = provider.perform(MediaAction.SHUFFLE)
                self.assertEqual(dispatched.action, MediaAction.SHUFFLE)
                arguments = runner.calls[-1][0]
                self.assertEqual(
                    arguments[arguments.index("--method") + 1],
                    "org.freedesktop.DBus.Properties.Set",
                )
                self.assertEqual(
                    arguments[-3:],
                    ("org.mpris.MediaPlayer2.Player", "Shuffle", variant),
                )

    def test_repeat_cycles_none_track_playlist_none(self):
        expected = (
            ("None", "Track"),
            ("Track", "Playlist"),
            ("Playlist", "None"),
        )
        for old, new in expected:
            with self.subTest(old=old, new=new):
                provider, runner = mpris([
                    names(PLAYER), status("Playing"), boolean(True),
                    loop_status(old), result(),
                ])
                dispatched = provider.perform(MediaAction.REPEAT)
                self.assertEqual(dispatched.action, MediaAction.REPEAT)
                self.assertEqual(
                    runner.calls[-1][0][-3:],
                    ("org.mpris.MediaPlayer2.Player", "LoopStatus",
                     f"<'{new}'>"),
                )

    def test_state_and_set_property_responses_fail_closed(self):
        cases = (
            [names(PLAYER), status("Playing"), result(b"(<1>,)")],
            [names(PLAYER), status("Playing"), boolean(True),
             loop_status("Invalid")],
            [names(PLAYER), status("Playing"), boolean(True), boolean(False),
             result(b"unexpected")],
        )
        operations = (
            lambda provider: provider.read_state(),
            lambda provider: provider.read_state(),
            lambda provider: provider.perform(MediaAction.SHUFFLE),
        )
        for responses, operation in zip(cases, operations):
            with self.subTest(responses=responses):
                provider, _ = mpris(responses)
                with self.assertRaises(MediaCommandFailed):
                    operation(provider)


class AppleMusicMediaProviderTests(unittest.TestCase):
    def provider(self, responses):
        runner = CommandFixture(responses)
        return AppleMusicMediaProvider(
            find_executable=lambda name: "/usr/bin/osascript",
            runner=runner,
        ), runner

    def test_every_action_uses_a_fixed_applescript_and_never_requests_launch(self):
        expected = {
            MediaAction.PLAY_PAUSE: "playpause",
            MediaAction.NEXT: "next track",
            MediaAction.PREVIOUS: "previous track",
            MediaAction.STOP: "stop",
        }
        for action, command in expected.items():
            with self.subTest(action=action):
                provider, runner = self.provider([result(b"swarm2:ok\n")])
                dispatched = provider.perform(action)
                self.assertEqual(
                    (dispatched.backend, dispatched.player_id, dispatched.prior_status),
                    ("apple-music", "com.apple.Music", None),
                )
                arguments = runner.calls[0][0]
                self.assertEqual(arguments[:2], ("/usr/bin/osascript", "-e"))
                script = arguments[2]
                self.assertIn('application id "com.apple.Music" is running', script)
                self.assertIn(
                    'tell application id "com.apple.Music" to ' + command,
                    script,
                )
                self.assertNotIn("activate", script.casefold())
                self.assertNotIn("launch", script.casefold())

    def test_discovery_checks_running_state_without_action_or_launch(self):
        provider, runner = self.provider([result(b"swarm2:running\n")])
        self.assertEqual(provider.available_players(), (
            MediaPlayerInfo(APPLE_MUSIC_PLAYER_ID, "Apple Music",
                            "apple-music"),
        ))
        script = runner.calls[0][0][2]
        self.assertIn('application id "com.apple.Music" is running', script)
        self.assertNotIn("tell application", script)
        self.assertNotIn("activate", script.casefold())

        provider, _ = self.provider([result(b"swarm2:no-player\n")])
        self.assertEqual(provider.available_players(), ())

        provider, _ = self.provider([result(b"unexpected")])
        with self.assertRaises(MediaCommandFailed):
            provider.available_players()

    def test_not_running_permission_denial_and_malformed_output_are_explicit(self):
        provider, _ = self.provider([result(b"swarm2:no-player\n")])
        with self.assertRaises(NoMediaPlayer):
            provider.perform(MediaAction.NEXT)

        provider, _ = self.provider([
            result(code=1, errors=b"Not authorized to send Apple events. (-1743)")
        ])
        with self.assertRaises(MediaPermissionDenied):
            provider.perform(MediaAction.NEXT)

        provider, _ = self.provider([result(b"anything else")])
        with self.assertRaises(MediaCommandFailed):
            provider.perform(MediaAction.NEXT)

    def test_connection_invalid_is_classified_as_player_disappearance(self):
        connection_invalid = result(
            code=1,
            errors=b"execution error: Connection is invalid. (-609)")
        provider, _ = self.provider([connection_invalid])
        with self.assertRaisesRegex(NoMediaPlayer, "closed before the action"):
            provider.perform(MediaAction.NEXT)

        provider, _ = self.provider([connection_invalid])
        with self.assertRaisesRegex(NoMediaPlayer, "state was read"):
            provider.read_state()

    def test_invalid_action_and_missing_osascript_run_nothing(self):
        provider, runner = self.provider([])
        with self.assertRaises(TypeError):
            provider.perform("next")
        self.assertEqual(runner.calls, [])

        runner = CommandFixture()
        provider = AppleMusicMediaProvider(
            find_executable=lambda name: None, runner=runner)
        with self.assertRaises(MediaProviderUnavailable):
            provider.perform(MediaAction.NEXT)
        self.assertEqual(runner.calls, [])

    def test_shuffle_and_repeat_use_fixed_nonlaunching_music_properties(self):
        for action, fragments in (
            (MediaAction.SHUFFLE,
             ("set shuffle enabled to not (shuffle enabled)",)),
            (MediaAction.REPEAT,
             ("if song repeat is off", "set song repeat to one",
              "set song repeat to all", "set song repeat to off")),
        ):
            with self.subTest(action=action):
                provider, runner = self.provider([result(b"swarm2:ok\n")])
                self.assertEqual(provider.perform(action).action, action)
                script = runner.calls[0][0][2]
                for fragment in fragments:
                    self.assertIn(fragment, script)
                self.assertIn(
                    'application id "com.apple.Music" is running', script)
                self.assertNotIn("activate", script.casefold())
                self.assertNotIn("launch", script.casefold())

    def test_read_state_maps_apple_music_flags_without_launching(self):
        provider, runner = self.provider([
            result(b"swarm2:state|playing|true|all\n")])
        self.assertEqual(provider.read_state(), MediaPlaybackState(
            True, True, MediaLoopStatus.PLAYLIST,
            "apple-music", APPLE_MUSIC_PLAYER_ID))
        script = runner.calls[0][0][2]
        self.assertIn("player state as text", script)
        self.assertIn("shuffle enabled as text", script)
        self.assertIn("song repeat as text", script)
        self.assertNotIn("activate", script.casefold())
        self.assertNotIn("launch", script.casefold())

        for response in (
            b"swarm2:no-player\n",
            b"swarm2:state|buffering|true|all\n",
            b"swarm2:state|playing|yes|all\n",
            b"swarm2:state|playing|true|context\n",
            b"unexpected\n",
        ):
            with self.subTest(response=response):
                provider, _ = self.provider([result(response)])
                error = NoMediaPlayer if response.startswith(
                    b"swarm2:no-player") else MediaCommandFailed
                with self.assertRaises(error):
                    provider.read_state()


class MediaPlaybackStateTests(unittest.TestCase):
    def test_model_is_strict_and_repeat_active_collapses_two_repeat_modes(self):
        self.assertFalse(MediaPlaybackState(
            False, False, MediaLoopStatus.NONE, "mpris", PLAYER).repeat_active)
        for mode in (MediaLoopStatus.TRACK, MediaLoopStatus.PLAYLIST):
            self.assertTrue(MediaPlaybackState(
                False, False, mode, "mpris", PLAYER).repeat_active)
        for invalid in (0, 1, None, "true"):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                MediaPlaybackState(
                    invalid, False, MediaLoopStatus.NONE, "mpris", PLAYER)
        with self.assertRaises(TypeError):
            MediaPlaybackState(False, False, "None", "mpris", PLAYER)

class HostMediaProviderBoundaryTests(unittest.TestCase):
    def test_factory_selects_platform_without_running_a_command(self):
        runner = CommandFixture()
        find_calls = []

        def find(name):
            find_calls.append(name)
            return "/usr/bin/" + name

        linux = create_host_media_provider(
            system="Linux", find_executable=find, runner=runner)
        macos = create_host_media_provider(
            system="Darwin", find_executable=find, runner=runner)
        other = create_host_media_provider(
            system="Windows", find_executable=find, runner=runner)
        self.assertIsInstance(linux, MprisMediaProvider)
        self.assertIsInstance(macos, AppleMusicMediaProvider)
        self.assertIsInstance(other, UnavailableMediaProvider)
        self.assertEqual(find_calls, ["gdbus", "osascript"])
        self.assertEqual(runner.calls, [])
        with self.assertRaises(MediaProviderUnavailable):
            other.perform(MediaAction.STOP)

    def test_mpris_preference_is_rejected_for_non_linux_factory(self):
        for system in ("Darwin", "Windows"):
            with self.subTest(system=system), self.assertRaises(ValueError):
                create_host_media_provider(
                    system=system, preferred_player=PLAYER)

        provider = create_host_media_provider(
            system="Darwin", preferred_player=APPLE_MUSIC_PLAYER_ID,
            find_executable=lambda name: "/usr/bin/osascript",
            runner=CommandFixture(),
        )
        self.assertIsInstance(provider, AppleMusicMediaProvider)

    def test_discovery_factory_uses_platform_provider_without_usb(self):
        runner = CommandFixture([names(PLAYER)])
        self.assertEqual(available_media_players(
            system="Linux",
            find_executable=lambda name: "/usr/bin/gdbus",
            runner=runner,
        ), (MediaPlayerInfo(PLAYER, "example", "mpris"),))
        self.assertEqual(len(runner.calls), 1)

    def test_injected_timeout_output_limit_and_wrong_result_are_bounded(self):
        cases = (
            (subprocess.TimeoutExpired(("gdbus",), 1), MediaCommandTimedOut),
            (_CommandOutputLimit(), MediaCommandFailed),
            (object(), MediaCommandFailed),
            (MediaCommandResult(0, b"x" * (MAX_COMMAND_OUTPUT_BYTES + 1), b""),
             MediaCommandFailed),
        )
        for response, error in cases:
            with self.subTest(response=type(response).__name__):
                provider, _ = mpris([response])
                with self.assertRaises(error):
                    provider.perform(MediaAction.STOP)

    def test_multiple_calls_share_one_total_dispatch_deadline(self):
        provider, runner = mpris([
            names(PLAYER), status("Playing"), boolean(True), boolean(True),
            result(),
        ])
        with patch(
                "swarm2.host_media.time.monotonic",
                side_effect=(0.0, 0.0, 0.4, 0.8, 1.2, 1.6, 1.8, 2.1)):
            with self.assertRaises(MediaCommandTimedOut):
                provider.perform(MediaAction.PLAY_PAUSE)
        self.assertEqual(DISPATCH_TIMEOUT_SECONDS, 2.0)
        self.assertEqual(len(runner.calls), 3)
        self.assertAlmostEqual(runner.calls[-1][1], 0.4)

    def test_runner_success_after_aggregate_deadline_is_still_a_timeout(self):
        provider, runner = mpris([names(PLAYER)])
        with patch(
                "swarm2.host_media.time.monotonic",
                side_effect=(0.0, 0.0, 2.1)):
            with self.assertRaises(MediaCommandTimedOut):
                provider.perform(MediaAction.STOP)
        self.assertEqual(len(runner.calls), 1)

    def test_real_runner_enforces_time_and_output_limits(self):
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
