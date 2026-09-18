import unittest
from types import SimpleNamespace
from unittest.mock import patch
from swarm2.firmware_commands import FirmwareProtocolError, decode_version_response
from swarm2.firmware_update import FirmwareUpdateError, inspect_update, run_update
from tests.test_firmware_commands import OFFER, VERSION, payload


class FakeFirmwareTransport:
    def __init__(self):
        self.version = VERSION
        self.writes = []
        self.events = []
        self.offer_status = 1
        self.content_status = 0
        self.corrupt_sequence = False
        self.drop_content = False
        self.fail_write = False
        self.flood = False
        self.begun = False
        self.access_checked = False

    def check_update_access(self):
        self.access_checked = True

    def begin_update(self):
        self.begun = True

    def get_feature(self, report_id, length):
        assert (report_id,length) == (0x2A,64)
        return self.version

    def set_output(self, report):
        assert self.begun
        self.writes.append(report)
        if self.fail_write:
            raise OSError('uncertain syscall')
        event = bytearray(17)
        if report[0] == 0x2D:
            event[0] = 0x2D; event[4] = report[4]; event[13] = self.offer_status
        else:
            if self.drop_content: return
            event[0] = 0x2C; event[1:3] = report[3:5]; event[5] = self.content_status
            if self.corrupt_sequence: event[1] ^= 1
        self.events.append(bytes(event))

    def read_input(self, timeout):
        if self.events: return self.events.pop(0)
        return b'\x10' if self.flood else b''


class FirmwareUpdateTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeFirmwareTransport()
        self.package = SimpleNamespace(offer=OFFER,payload=payload(3))

    def test_success_is_transfer_only_not_installed_version_verification(self):
        events = []
        result = run_update(self.transport,self.package,events.append)
        self.assertEqual(result['outcome'],'transferred')
        self.assertFalse(result['version_verified'])
        self.assertEqual(result['records_acknowledged'],3)
        self.assertEqual([r[0] for r in self.transport.writes],[0x2D,0x2A,0x2A,0x2A])
        self.assertEqual(events[-1]['phase'],'transferred')
        self.assertTrue(self.transport.begun)

    def test_claim_failure_stops_before_version_query_or_offer(self):
        with patch.object(self.transport, 'begin_update', side_effect=OSError('interface busy')), \
             patch.object(self.transport, 'get_feature') as get_feature:
            with self.assertRaisesRegex(FirmwareUpdateError, 'No firmware data was sent') as caught:
                run_update(self.transport, self.package)
        get_feature.assert_not_called()
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.transport.writes, [])

    def test_lost_offer_ack_reports_no_content_sent(self):
        with patch.object(self.transport, 'read_input', return_value=b''):
            with self.assertRaisesRegex(FirmwareUpdateError, 'No firmware data was sent') as caught:
                run_update(self.transport, self.package)
        self.assertEqual(caught.exception.phase, 'offer')
        self.assertEqual(caught.exception.content_records_sent, 0)
        self.assertEqual([report[0] for report in self.transport.writes], [0x2D])

    def test_old_pending_events_drained_before_offer(self):
        self.transport.events = [bytes([0x2C])+bytes(16)]
        self.assertEqual(run_update(self.transport,self.package)['records_acknowledged'],3)

    def test_invalid_input_never_sends_offer(self):
        for version in [b'bad',VERSION[:4]+b'\x02'+VERSION[5:],VERSION[:10]+b'\x0f'+VERSION[11:]]:
            with self.subTest(version=version):
                self.transport.version = version
                with self.assertRaises(FirmwareUpdateError) as caught:
                    run_update(self.transport,self.package)
                self.assertFalse(caught.exception.device_may_have_changed)
                self.assertEqual(self.transport.writes,[])

    def test_same_version_prevents_flash(self):
        data = bytearray(VERSION); data[5:9] = OFFER[4:8]
        self.transport.version = bytes(data)
        with self.assertRaises(FirmwareUpdateError): run_update(self.transport,self.package)
        self.assertEqual(self.transport.writes,[])

    def test_corrupt_payload_never_sends_offer(self):
        self.package.payload += b'x'
        with self.assertRaises(FirmwareUpdateError): run_update(self.transport,self.package)
        self.assertEqual(self.transport.writes,[])

    def test_rejection_or_busy_sends_no_content_and_never_retries(self):
        for status in (0,2,3,255):
            transport = FakeFirmwareTransport(); transport.offer_status = status
            with self.subTest(status=status), self.assertRaises(FirmwareUpdateError) as caught:
                run_update(transport,self.package)
            self.assertEqual(len(transport.writes),1)
            self.assertEqual(caught.exception.records_acknowledged,0)

    def test_lost_content_ack_never_replays_packet(self):
        self.transport.drop_content = True
        with self.assertRaises(FirmwareUpdateError) as caught:
            run_update(self.transport,self.package)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(self.transport.writes),2)
        self.assertEqual(caught.exception.records_acknowledged,0)
        self.assertEqual(caught.exception.content_records_sent, 1)
        self.assertNotIn('No firmware data was sent', str(caught.exception))

    def test_sequence_mismatch_and_device_error_stop(self):
        for mode in ('corrupt_sequence','content_status'):
            transport = FakeFirmwareTransport(); setattr(transport,mode,True)
            with self.subTest(mode=mode), self.assertRaises(FirmwareUpdateError):
                run_update(transport,self.package)
            self.assertEqual(len(transport.writes),2)

    def test_failed_first_write_is_uncertain(self):
        self.transport.fail_write = True
        with self.assertRaises(FirmwareUpdateError) as caught:
            run_update(self.transport,self.package)
        self.assertTrue(caught.exception.device_may_have_changed)
        self.assertEqual(len(self.transport.writes),1)

    def test_nonidle_queue_bounded_before_any_write(self):
        self.transport.flood = True
        with self.assertRaises(FirmwareUpdateError) as caught:
            run_update(self.transport,self.package)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.transport.writes,[])

    def test_timeout_before_offer_preserves_state(self):
        with patch('swarm2.firmware_update.time.monotonic',side_effect=[0,1801]):
            with self.assertRaises(FirmwareUpdateError) as caught:
                run_update(self.transport,self.package)
        self.assertFalse(caught.exception.device_may_have_changed)
        self.assertEqual(self.transport.writes,[])

    def test_response_deadline_exceeding_native_transport_limit_fails_before_io(self):
        with self.assertRaisesRegex(FirmwareUpdateError, 'time limits'):
            run_update(self.transport, self.package, response_timeout=6)
        self.assertEqual(self.transport.writes, [])

    def test_broken_progress_callback_does_not_interrupt_firmware(self):
        def callback(event):
            if event['phase'] == 'transfer': raise OSError('broken progress pipe')
        result = run_update(self.transport,self.package,callback)
        self.assertEqual(result['outcome'], 'transferred')
        self.assertEqual(result['records_acknowledged'], 3)
        self.assertEqual(len(self.transport.writes), 4)

    def test_inspect_realtek_branch(self):
        plan = inspect_update(decode_version_response(VERSION),self.package)
        self.assertTrue(plan['supported'])
        self.assertFalse(plan['cancellation_supported'])
        self.assertEqual(plan['bank'],2)
        self.assertEqual(plan['device_component_marker'],0)
