"""Profile/energy packing uses captured layout and independent checksum vectors."""

from dataclasses import replace
import unittest

from swarm2.display_commands import (
    build_application_report, build_profile_read_request, decode_profile_response,
)
from swarm2.protocol import ProtocolError
from swarm2.settings import stable


def profile_response(current=0, count=5, energy=False):
    packed = count | (0x10 if energy else 0)
    return bytes((0x10, 0x12, 0, current, packed, (-current-packed) & 255))


class DisplayCommandTests(unittest.TestCase):
    def test_captured_profile_and_read_selector(self):
        raw = bytes.fromhex("1012000005fb")
        state = decode_profile_response(raw)
        self.assertEqual((state.current_profile, state.profile_count, state.energy_saving), (0, 5, False))
        self.assertEqual(state.raw, raw)
        self.assertEqual(build_profile_read_request(), bytes.fromhex("101c0012000000") + bytes(57))

    def test_every_valid_count_current_profile_and_energy_flag(self):
        for count in range(1, 6):
            for current in range(count):
                for energy in (False, True):
                    with self.subTest(count=count, current=current, energy=energy):
                        state = decode_profile_response(profile_response(current, count, energy))
                        self.assertEqual((state.current_profile, state.profile_count, state.energy_saving),
                                         (current, count, energy))
                        for active in (False, True):
                            packet = build_application_report(state, active)
                            self.assertEqual(packet[2], current | (0x80 if active else 0))
                            self.assertEqual(packet[3], count | (0x10 if energy else 0))
                            self.assertEqual(sum(packet[1:5]) & 255, 0)
                            self.assertEqual(packet[5:], bytes(59))

    def test_activation_switch_and_energy_golden_vectors(self):
        state = decode_profile_response(profile_response())
        self.assertEqual(build_application_report(state, True), bytes.fromhex("1012800569") + bytes(59))
        self.assertEqual(build_application_report(state, True, profile_index=4, energy_saving=True),
                         bytes.fromhex("1012841555") + bytes(59))
        self.assertEqual(build_application_report(state, False, profile_index=4),
                         bytes.fromhex("10120405e5") + bytes(59))
        self.assertEqual(state.raw, bytes.fromhex("1012000005fb"))

    def test_switch_preserves_count_and_energy_by_default(self):
        state = decode_profile_response(profile_response(1, 3, True))
        packet = build_application_report(state, True, profile_index=2)
        self.assertEqual(packet[:5], bytes.fromhex("1012821359"))
        self.assertEqual(build_application_report(state, True, energy_saving=False)[3], 3)

    def test_live_f0_energy_readback_is_normalized_without_losing_raw_evidence(self):
        # Actual GET after 10 12 80 15 59, then restored to 10 12 00 00 05 fb.
        # Vendor 0.0.0.7 tests high nibble == 10; this firmware returns f0.
        captured = bytes.fromhex("10120000f50b")
        state = decode_profile_response(captured)
        self.assertTrue(state.energy_saving)
        self.assertEqual(state.raw, captured)
        self.assertEqual(state.profile_count, 5)
        self.assertEqual(build_application_report(state, True), bytes.fromhex("1012801559") + bytes(59))
        self.assertEqual(build_application_report(state, True, energy_saving=False),
                         bytes.fromhex("1012800569") + bytes(59))
        self.assertEqual(stable("profile", captured), stable("profile", profile_response(0, 5, True)))
        self.assertNotEqual(stable("profile", captured), stable("profile", profile_response(0, 5, False)))
        self.assertNotEqual(stable("profile", captured), stable("profile", profile_response(1, 5, True)))
        self.assertNotEqual(stable("profile", captured), stable("profile", profile_response(0, 4, True)))

    def test_zero_padded_transport_buffer_is_normalized_but_unknown_tail_is_rejected(self):
        raw = profile_response(2, 5, True)
        self.assertEqual(decode_profile_response(raw + bytes(58)).raw, raw)
        with self.assertRaises(ProtocolError):
            decode_profile_response(raw + bytes(57) + b"\x01")

    def test_bad_identity_checksum_counts_flags_and_truncation_are_rejected(self):
        valid = profile_response()
        for raw in (valid[:5], valid + b"\0", valid + bytes(59),
                    b"\x11" + valid[1:], valid[:1] + b"\x13" + valid[2:],
                    valid[:2] + b"\x01" + valid[3:], valid[:-1] + b"\x00",
                    profile_response(0, 0), profile_response(0, 6),
                    profile_response(5, 5), profile_response(2, 2)):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                decode_profile_response(raw)
        for packed in (0x25, 0x35, 0x85):
            raw = bytes((0x10, 0x12, 0, 0, packed, -packed & 255))
            with self.subTest(packed=packed), self.assertRaises(ProtocolError):
                decode_profile_response(raw)

    def test_write_input_types_and_profile_count_are_enforced(self):
        state = decode_profile_response(profile_response(0, 2))
        for index in (-1, 2, 4, True, 1.0, "1"):
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                build_application_report(state, True, profile_index=index)
        for invalid in (0, 1, None, "true"):
            with self.subTest(active=invalid), self.assertRaises(ProtocolError):
                build_application_report(state, invalid)
        for invalid in (0, 1, "true"):
            with self.subTest(energy=invalid), self.assertRaises(ProtocolError):
                build_application_report(state, True, energy_saving=invalid)

    def test_write_revalidates_baseline_bytes_and_uses_their_preserved_fields(self):
        state = decode_profile_response(profile_response(1, 3, True))
        with self.assertRaises(ProtocolError):
            build_application_report(replace(state, raw=state.raw[:-1] + b"\0"), True)
        # Derived fields cannot smuggle another current/count into a valid raw baseline.
        altered = replace(state, current_profile=4, profile_count=5, energy_saving=False)
        self.assertEqual(build_application_report(altered, True), build_application_report(state, True))


if __name__ == "__main__":
    unittest.main()
