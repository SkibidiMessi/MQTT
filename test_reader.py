"""Offline checks for unit conversion, sentinels, identities and staleness."""
import json
import math
import struct
import unittest
from types import SimpleNamespace
from telemetry_reader import Reader, ROOT, decode_parameter, px4_mode


class Message(SimpleNamespace):
    def get_type(self): return self.kind
    def get_srcSystem(self): return getattr(self, 'sysid', 1)
    def get_srcComponent(self): return getattr(self, 'compid', 1)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.fields = json.loads((ROOT / 'fields.json').read_text())
        self.r = Reader(self.fields)

    def heartbeat(self, now=10):
        self.r.ingest(Message(kind='HEARTBEAT', autopilot=12, base_mode=128,
                              custom_mode=(4 << 16) | (3 << 24), system_status=4), now)

    def test_source_filter_and_stale_heartbeat(self):
        self.r.ingest(Message(kind='HEARTBEAT', sysid=2, autopilot=12), 10)
        self.assertFalse(self.r.snapshot(10, 'test', 0)['px4_connected'])
        self.heartbeat()
        self.assertTrue(self.r.snapshot(11, 'test', 0)['armed'])
        old = self.r.snapshot(14, 'test', 1)
        self.assertFalse(old['px4_connected'])
        self.assertIsNone(old['armed'])
        self.assertEqual(old['px4_heartbeat_age'], 4000)

    def test_units_sign_and_yaw(self):
        self.heartbeat()
        self.r.ingest(Message(kind='GLOBAL_POSITION_INT', lat=210123456, lon=1051234567,
                              alt=123450, relative_alt=2000, vx=300, vy=400, vz=-25), 10)
        self.r.ingest(Message(kind='ATTITUDE', roll=math.pi/6, pitch=-math.pi/12, yaw=-math.pi/2), 10)
        out = self.r.snapshot(10, 'test', 0)
        self.assertAlmostEqual(out['latitude'], 21.0123456)
        self.assertAlmostEqual(out['longitude'], 105.1234567)
        self.assertEqual(out['ground_speed'], 5)
        self.assertEqual(out['vertical_speed'], .25)
        self.assertEqual(out['altitude_msl'], 123.45)
        self.assertAlmostEqual(out['yaw'], 270)

    def test_battery_unknown_and_wrong_battery(self):
        self.r.ingest(Message(kind='BATTERY_STATUS', id=1), 10)
        self.assertIsNone(self.r.snapshot(10, 'test', 0)['battery_voltage'])
        self.r.ingest(Message(kind='BATTERY_STATUS', id=0, voltages=[65535]*10,
                              current_battery=-1, battery_remaining=-1, charge_state=0), 10)
        out = self.r.snapshot(10, 'test', 0)
        for key in ('battery_voltage', 'battery_current', 'battery_remaining', 'battery_warning'):
            self.assertIsNone(out[key])
        self.r.ingest(Message(kind='BATTERY_STATUS', id=0, voltages=[4000]*6+[65535]*4,
                              current_battery=1250, battery_remaining=80, charge_state=2), 11)
        out = self.r.snapshot(11, 'test', 0)
        self.assertEqual(out['battery_voltage'], 24)
        self.assertEqual(out['battery_current'], 12.5)
        self.assertEqual(out['battery_warning'], 1)
        self.assertIsNone(self.r.snapshot(22, 'test', 0)['battery_remaining'])

    def test_position_validity_and_unknown_dop(self):
        self.heartbeat()
        self.r.ingest(Message(kind='GLOBAL_POSITION_INT', lat=0, lon=0, alt=0, relative_alt=0,
                              vx=0, vy=0, vz=0), 10)
        self.assertIsNone(self.r.snapshot(10, 'test', 0)['position_valid'])
        self.r.ingest(Message(kind='ESTIMATOR_STATUS', flags=16|32), 10)
        self.assertTrue(self.r.snapshot(10, 'test', 0)['position_valid'])
        self.r.ingest(Message(kind='ESTIMATOR_STATUS', flags=16|32|1024), 10)
        self.assertFalse(self.r.snapshot(10, 'test', 0)['position_valid'])
        self.r.ingest(Message(kind='GPS_RAW_INT', fix_type=3, satellites_visible=255, eph=80, epv=120), 10)
        out=self.r.snapshot(10, 'test', 0)
        self.assertIsNone(out['hdop'])
        self.assertIsNone(out['vdop'])
        self.assertIsNone(out['satellites_visible'])

    def test_px4_bytewise_parameters_and_complete_schema(self):
        wire = struct.unpack('<f', struct.pack('<i', 6))[0]
        msg=Message(kind='PARAM_VALUE', param_id='BAT1_N_CELLS', param_type=6,
                    param_value=wire, param_count=1, param_index=0)
        self.assertEqual(decode_parameter(msg), 6)
        self.r.ingest(msg, 10)
        self.assertEqual(self.r.parameters['BAT1_N_CELLS']['value'], 6)
        out = self.r.snapshot(10, 'test', 0)
        self.assertEqual(set(out), {f['name'] for f in self.fields})
        self.assertEqual(len(out), 58)
        for key in ('failsafe_active', 'failsafe_reason', 'mission_status', 'mqtt_connected'):
            self.assertIsNone(out[key])
        json.dumps(out, allow_nan=False)

    def test_enum_mapping(self):
        self.assertEqual(px4_mode((4 << 16) | (4 << 24)), 3)
        self.assertEqual(px4_mode(6 << 16), 6)
        self.assertEqual(px4_mode(2 << 16), 255)


if __name__ == '__main__':
    unittest.main()
