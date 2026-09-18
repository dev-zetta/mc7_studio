"""Serialized background transfer with a correlated response for every packet."""

import base64
import binascii
import time

from .image_commands import (BACKGROUND_RGBA_BYTES, build_background_transfer,
    decode_image_response, encode_background_rgba, build_background_selection_read_request,
    decode_background_selection_response, build_background_selection_report, CUSTOM_BACKGROUND_INDEX)
from .status_commands import build_status_read_request, decode_status_response
from .transport import DeviceError


def upload_background(request, transport):
    encoded = request.get('rgba')
    if not isinstance(encoded, str) or len(encoded) != (BACKGROUND_RGBA_BYTES + 2) // 3 * 4:
        raise DeviceError('Background image has an invalid size')
    try:
        rgba = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise DeviceError('Invalid background image encoding') from error
    plan = build_background_transfer(encode_background_rgba(rgba))
    # Confirm the opened mouse responds before beginning a global image change.
    transport.send(build_status_read_request())
    before = decode_status_response(transport.get_feature(9))
    transport.send(build_background_selection_read_request())
    selection_before = decode_background_selection_response(transport.get_feature(0x2C))
    deadline = time.monotonic() + 110
    completed = 0
    try:
        for step in plan.steps:
            if time.monotonic() >= deadline:
                raise DeviceError('Background transfer exceeded its deadline')
            reply = transport.exchange_image(step.report, delay_ms=step.minimum_delay_ms)
            decode_image_response(reply, expected_command=step.command)
            completed += 1
        # The image data and the active background choice are separate settings.
        transport.send(build_background_selection_report(CUSTOM_BACKGROUND_INDEX))
        transport.send(build_background_selection_read_request())
        selection_after = decode_background_selection_response(transport.get_feature(0x2C))
        if selection_after.background_index != CUSTOM_BACKGROUND_INDEX:
            raise DeviceError('Custom background selection did not match the requested setting')
        transport.send(build_status_read_request())
        after = decode_status_response(transport.get_feature(9))
        if after.firmware_version != before.firmware_version:
            raise DeviceError('Device identity changed during background transfer')
    except (OSError, ValueError, DeviceError) as error:
        raise DeviceError(f'Background transfer stopped after {completed}/{len(plan.steps)} packets: {error}. '
                          'The background may be incomplete. No automatic replay was attempted.') from error
    return {'acknowledged': True, 'pixel_readback': False, 'image_sha256': plan.image_sha256,
            'image_bytes': plan.image_bytes, 'completed_packets': completed,
            'scope': 'all_profiles', 'firmware_version': after.firmware_version,
            'selection_verified': True, 'selection_before': selection_before.raw.hex(),
            'selection_after': selection_after.raw.hex()}
