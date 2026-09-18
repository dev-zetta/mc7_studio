import struct
import unittest
import json
from pathlib import Path
from swarm2.firmware_commands import (
    FirmwareProtocolError, build_offer_report, decode_content_response,
    decode_offer_response, decode_version_response, iter_content_reports,
    parse_offer, validate_payload, decode_realtek_version,
)

OFFER = bytes.fromhex('00000f00715000480000000004000000')
VERSION = bytes.fromhex('2a010000047150002062000000').ljust(61, b'\0')


def payload(count=2, base=0):
    return b''.join(struct.pack('<IB', base + i * 52, 52) + bytes([i % 256]) * 52 for i in range(count))


class FirmwareCommandTests(unittest.TestCase):

    def test_matches_reference_command_vectors(self):
        path = Path(__file__).resolve().parent / 'fixtures/firmware-command-vectors.json'
        evidence = json.loads(path.read_text(encoding='utf-8'))
        expected = [bytes.fromhex(report) for report in evidence['reports']]
        actual = [build_offer_report(bytes.fromhex(evidence['offer'])),
                  *iter_content_reports(payload(3, 0x4000))]
        self.assertEqual(actual, expected)
        for packed, parts in evidence['versions'].items():
            self.assertEqual(decode_realtek_version(int(packed, 16)), tuple(parts))

    def test_actual_offer_and_version_layout(self):
        offer = parse_offer(OFFER)
        assert (offer.component_id, offer.version_raw, offer.token) == (15, 0x48005071, 0)
        version = decode_version_response(VERSION)
        assert (version.component_id, version.version_raw, version.bank) == (0, 0x20005071, 2)
        assert version.protocol_version == 4
        assert decode_version_response(VERSION + bytes(3)).version_raw == version.version_raw


    def test_wrong_feature_cached_ordinary_settings_is_rejected(self):
        cached = bytes.fromhex('2b09000405204019ff64011a') + bytes(52)
        with self.assertRaises(FirmwareProtocolError):
            decode_version_response(cached)
        with self.assertRaises(FirmwareProtocolError):
            decode_version_response(b'\x2a' + cached[1:])


    def test_version_rejects_unknown_headers_and_padding(self):
        offset, value = 4, 2
        data = bytearray(VERSION + bytes(3)); data[offset] = value
        with self.assertRaises(FirmwareProtocolError):
            decode_version_response(bytes(data))


    def test_source_exact_offer_flags_and_padding(self):
        assert build_offer_report(OFFER) == bytes.fromhex('2d00400f00715000480000000004000000') + bytes(44)
        changed = bytearray(OFFER); changed[1] = 0x8F; changed[10] = 0xFF
        wire = build_offer_report(bytes(changed))
        assert wire[2] == 0x4F and wire[11] == 0xFB
        assert OFFER[1] == 0


    def test_payload_and_reports_relative_addresses(self):
        data = payload(3, 0x4000)
        info = validate_payload(data)
        assert (info.record_count, info.data_bytes, info.base_address, info.end_address) == (3,156,0x4000,0x409C)
        reports = list(iter_content_reports(data))
        assert all(len(report) == 61 for report in reports)
        assert [report[1] for report in reports] == [0x80,0,0x40]
        assert [int.from_bytes(report[5:9], 'little') for report in reports] == [0,52,104]
        assert reports[1] == bytes.fromhex('2a0034010034000000') + bytes([1]) * 52


    def test_sequence_wrap_matches_vendor_uint16(self):
        reports = iter_content_reports(payload(65538))
        selected = {index: report for index, report in enumerate(reports) if index >= 65535}
        assert [int.from_bytes(report[3:5], 'little') for report in selected.values()] == [65535,0,1]
        assert selected[65537][1] == 0x40


    def test_bad_payload_fails_before_streaming(self):
        for data in [b'', b'x', struct.pack('<IB',0,0)+bytes(52), struct.pack('<IB',0,53)+bytes(52),
                     struct.pack('<IB',0xFFFFFFF0,52)+bytes(52), payload()+payload()]:
            with self.subTest(data=data[:20]), self.assertRaises(FirmwareProtocolError):
                validate_payload(data)


    def test_offer_and_content_response_correlations(self):
        offer = bytearray(17); offer[0]=0x2D; offer[13]=1
        assert decode_offer_response(bytes(offer)).status == 1
        offer[4] = 1
        with self.assertRaises(FirmwareProtocolError): decode_offer_response(bytes(offer))
        content = bytearray(17); content[0]=0x2C; content[1:3]=(456).to_bytes(2,'little')
        assert decode_content_response(bytes(content),sequence=456).status == 0
        with self.assertRaises(FirmwareProtocolError): decode_content_response(bytes(content),sequence=455)
        with self.assertRaises(FirmwareProtocolError): decode_content_response(bytes(content[:-1]),sequence=456)
