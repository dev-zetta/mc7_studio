import base64
import unittest
from unittest.mock import patch

from swarm2.background import upload_background
from swarm2.image_commands import BACKGROUND_RGBA_BYTES, build_background_selection_report
from swarm2.service import DeviceService
from swarm2.transport import DeviceError

STATUS = bytes.fromhex('1009000405204019ff63011b')

class ImageTransport:
    def __init__(self, fail_at=None, wrong_f1=False, ff_filled_data=False):
        self.steps=[]
        self.image_replies=[]
        self.selectors=[]
        self.operations=[]
        self.background_index=0
        self.ignore_selection=False
        self.status_reads=0
        self.status_after=STATUS
        self.fail_at=fail_at
        self.wrong_f1=wrong_f1
        self.ff_filled_data=ff_filled_data
    def send(self, report):
        self.selectors.append(report)
        self.operations.append(('send',report))
        if report[1]==0x2c and not self.ignore_selection:
            self.background_index=report[2]
    def get_feature(self, selector):
        self.operations.append(('get',selector))
        if selector==0x2c:
            return bytes((0x10,0x2c,0,self.background_index,0,0,0,0))
        assert selector==9
        self.status_reads+=1
        return STATUS if self.status_reads==1 else self.status_after
    def exchange_image(self, report, *, delay_ms):
        self.steps.append((report,delay_ms))
        self.operations.append(('image',report))
        if len(self.steps)==self.fail_at: raise DeviceError('Disconnected')
        if self.ff_filled_data and 1 <= report[2] <= 61:
            reply = bytes((0x10,)) + bytes((0xFF,)) * 63
            self.image_replies.append(reply)
            return reply
        selector = 0xa5
        command = report[2]
        if self.wrong_f1 and report[2] == 0xF1:
            command = 0xF0
        reply = bytes((0x10,selector,command))
        self.image_replies.append(reply)
        return reply

class BackgroundTests(unittest.TestCase):
    def request(self):
        return {'rgba':base64.b64encode(bytes(BACKGROUND_RGBA_BYTES)).decode('ascii')}
    def test_complete_transfer_is_echo_acknowledged_and_never_claims_pixel_readback(self):
        transport=ImageTransport()
        result=upload_background(self.request(),transport)
        self.assertTrue(result['acknowledged'])
        self.assertFalse(result['pixel_readback'])
        self.assertEqual(len(transport.steps),1122)
        self.assertEqual(result['completed_packets'],1122)
        self.assertEqual(result['scope'],'all_profiles')
        self.assertEqual(transport.steps[0][0][:3],b'\x10\xa5\0')
        self.assertEqual(transport.steps[-1][0][:3],b'\x10\xa5\xff')
        self.assertEqual(transport.steps[-1][1],2500)
        self.assertTrue(result['selection_verified'])
        self.assertEqual(result['selection_before'],'102c000000000000')
        self.assertEqual(result['selection_after'],'102c000100000000')
        writes=[p for p in transport.selectors if p[1]!=0x1c]
        self.assertEqual(writes,[build_background_selection_report(1)])
        selected=next(i for i,entry in enumerate(transport.operations)
                      if entry==('send',build_background_selection_report(1)))
        last_image=max(i for i,entry in enumerate(transport.operations) if entry[0]=='image')
        self.assertGreater(selected,last_image)
    def test_firmware_504_ff_filled_data_completion_finishes_the_full_transfer(self):
        transport=ImageTransport(ff_filled_data=True)
        result=upload_background(self.request(),transport)
        self.assertEqual(result['completed_packets'],1122)
        self.assertTrue(result['selection_verified'])
        self.assertEqual(sum(1 for report,_ in transport.steps if 1 <= report[2] <= 61),1088)
        first_data = next(index for index, (report, _) in enumerate(transport.steps)
                          if 1 <= report[2] <= 61)
        self.assertEqual(transport.image_replies[first_data],
                         bytes((0x10,)) + bytes((0xFF,)) * 63)
    def test_partial_failure_aborts_without_retry_or_finish(self):
        transport=ImageTransport(fail_at=6)
        with self.assertRaisesRegex(DeviceError,'5/1122.*background may be incomplete'):
            upload_background(self.request(),transport)
        self.assertEqual(len(transport.steps),6)
        self.assertNotEqual(transport.steps[-1][0][2],0xff)
        self.assertFalse(any(p[1]==0x2c for p in transport.selectors))
    def test_bad_f1_phase_aborts_before_any_image_data(self):
        transport=ImageTransport(wrong_f1=True)
        with self.assertRaisesRegex(DeviceError,'command echo does not match'):
            upload_background(self.request(),transport)
        self.assertEqual(len(transport.steps),2)
        self.assertFalse(any(p[1]==0x2c for p in transport.selectors))

    def test_deadline_aborts_without_later_packet_or_selection(self):
        transport=ImageTransport()
        with patch('swarm2.background.time.monotonic',side_effect=[0,0,111]):
            with self.assertRaisesRegex(DeviceError,'1/1122.*deadline'):
                upload_background(self.request(),transport)
        self.assertEqual(len(transport.steps),1)
        self.assertFalse(any(p[1]==0x2c for p in transport.selectors))

    def test_selection_readback_mismatch_never_reports_success_or_replays_image(self):
        transport=ImageTransport()
        transport.ignore_selection=True
        with self.assertRaisesRegex(DeviceError,'1122/1122.*selection did not match'):
            upload_background(self.request(),transport)
        self.assertEqual(len(transport.steps),1122)
        self.assertEqual([p for p in transport.selectors if p[1]==0x2c],
                         [build_background_selection_report(1)])

    def test_bad_initial_selection_response_prevents_any_image_write(self):
        transport=ImageTransport()
        with patch.object(transport,'get_feature',side_effect=[STATUS,b'\x10\x2b\0\0\0\0\0\0']):
            with self.assertRaisesRegex(ValueError,'Background selection report'):
                upload_background(self.request(),transport)
        self.assertEqual(transport.steps,[])
        self.assertFalse(any(p[1]==0x2c for p in transport.selectors))

    def test_battery_and_charge_change_do_not_fail_completed_image(self):
        transport=ImageTransport()
        status=bytearray(STATUS)
        status[9:11]=bytes((97,0))
        status[-1]=-sum(status[2:-1])&255
        transport.status_after=bytes(status)
        result=upload_background(self.request(),transport)
        self.assertTrue(result['acknowledged'])
        self.assertEqual(result['firmware_version'],'5.04')

    def test_firmware_identity_change_fails_without_replaying_or_claiming_success(self):
        transport=ImageTransport()
        status=bytearray(STATUS)
        status[4]=6
        status[-1]=-sum(status[2:-1])&255
        transport.status_after=bytes(status)
        with self.assertRaisesRegex(DeviceError,'1122/1122.*identity changed'):
            upload_background(self.request(),transport)
        self.assertEqual(len(transport.steps),1122)
    def test_invalid_input_never_opens_a_transfer(self):
        for data in (None,'',123,'!'*115115,'!'*115116):
            transport=ImageTransport()
            with self.subTest(data_type=type(data)),self.assertRaises((DeviceError,ValueError)):
                upload_background({'rgba':data},transport)
            self.assertFalse(transport.steps)
            self.assertFalse(transport.selectors)
    def test_service_deadlines_cover_bounded_transfer_and_multi_record_reads(self):
        with patch('swarm2.service.platform.system',return_value='Linux'),patch('swarm2.service.subprocess.run') as run:
            run.return_value.stdout='{"result":{}}';run.return_value.returncode=0
            DeviceService._call({'operation':'upload_background'})
            self.assertEqual(run.call_args.kwargs['timeout'],120)
            DeviceService._call({'operation':'read_settings'})
            self.assertEqual(run.call_args.kwargs['timeout'],60)
            DeviceService._call({'operation':'apply_settings'})
            self.assertEqual(run.call_args.kwargs['timeout'],90)
            DeviceService._call({'operation':'read'})
            self.assertEqual(run.call_args.kwargs['timeout'],20)

if __name__=='__main__':unittest.main()
