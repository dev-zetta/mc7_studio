"""Per-profile media player selection stays host-only and strictly bounded."""

import copy
import unittest

from swarm2.configuration import Configuration, ConfigurationError
from swarm2.media_player_ids import (
    APPLE_MUSIC_PLAYER_ID,
    MAX_MEDIA_PLAYER_ID_CHARACTERS,
    validate_media_player_id,
)


PLAYER = "org.mpris.MediaPlayer2.vlc"


class MediaPlayerIdTests(unittest.TestCase):
    def test_supported_player_ids_are_exact_and_bounded(self):
        self.assertEqual(validate_media_player_id(PLAYER), PLAYER)
        self.assertEqual(
            validate_media_player_id(APPLE_MUSIC_PLAYER_ID),
            APPLE_MUSIC_PLAYER_ID,
        )
        maximum = "org.mpris.MediaPlayer2." + "a" * (
            MAX_MEDIA_PLAYER_ID_CHARACTERS - len("org.mpris.MediaPlayer2."))
        self.assertEqual(validate_media_player_id(maximum), maximum)

        invalid = (
            None, "", "vlc", "org.mpris.MediaPlayer2.2bad",
            "org.mpris.MediaPlayer2.bad.name!", PLAYER + "\n",
            maximum + "a", 4,
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_media_player_id(value)

    def test_preset_roundtrip_and_old_version_one_default(self):
        configuration = Configuration()
        configuration.display.pages = [
            ["general_media", None, None, "dpi"],
        ]
        configuration.display.preferred_media_player = PLAYER
        restored = Configuration.from_dict(configuration.to_dict())
        self.assertEqual(restored, configuration)

        older = copy.deepcopy(configuration.to_dict())
        older["display"].pop("preferred_media_player")
        self.assertIsNone(
            Configuration.from_dict(older).display.preferred_media_player)

    def test_preset_rejects_unsupported_player_id(self):
        data = Configuration().to_dict()
        for value in ("vlc", "org.mpris.MediaPlayer2.bad;name", False, 3):
            with self.subTest(value=value):
                changed = copy.deepcopy(data)
                changed["display"]["preferred_media_player"] = value
                with self.assertRaisesRegex(ConfigurationError, "Media player"):
                    Configuration.from_dict(changed)


if __name__ == "__main__":
    unittest.main()
