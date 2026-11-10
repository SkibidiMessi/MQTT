"""Local protocol smoke test using a minimal MQTT test peer (not a full broker)."""
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from mqtt_publisher import Publisher
from lac_protocol import encode_telemetry, validate_device_id


class Peer:
    def __init__(self):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        self.server.listen(1)
        self.server.settimeout(.2)
        self.stop = threading.Event()
        self.messages = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn = None
        try:
            while not self.stop.is_set() and conn is None:
                try:
                    conn, _ = self.server.accept()
                    conn.settimeout(.2)
                except socket.timeout:
                    continue
            def read(n):
                data = b''
                while len(data) < n and not self.stop.is_set():
                    try:
                        chunk = conn.recv(n - len(data))
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise EOFError
                    data += chunk
                if len(data) != n:
                    raise EOFError
                return data
            while conn and not self.stop.is_set():
                first = read(1)[0]
                length, factor = 0, 1
                while True:
                    b = read(1)[0]
                    length += (b & 127) * factor
                    if b < 128:
                        break
                    factor *= 128
                body = read(length)
                kind = first >> 4
                if kind == 1:
                    conn.sendall(b'\x20\x02\x00\x00')
                elif kind == 3:
                    size = int.from_bytes(body[:2], 'big')
                    topic = body[2:2+size].decode()
                    offset = 2 + size
                    qos = (first >> 1) & 3
                    if qos:
                        packet = body[offset:offset+2]
                        offset += 2
                        conn.sendall(b'\x40\x02' + packet)
                    self.messages.append((topic, body[offset:].decode()))
                elif kind == 12:
                    conn.sendall(b'\xd0\x00')
                elif kind == 8:
                    packet = body[:2]
                    size = int.from_bytes(body[2:4], 'big')
                    topic = body[4:4+size]
                    conn.sendall(b'\x90\x03' + packet + b'\x01')
                    # A retained QoS0 control packet, observation only.
                    payload = b'c:setspeed:3;'
                    data = len(topic).to_bytes(2, 'big') + topic + payload
                    conn.sendall(b'\x31' + bytes([len(data)]) + data)
                elif kind == 14:
                    break
        except (OSError, EOFError):
            pass
        finally:
            if conn:
                conn.close()

    def close(self):
        self.stop.set()
        self.server.close()
        self.thread.join(2)


class MqttTests(unittest.TestCase):
    def test_real_paho_data_and_control_subscription(self):
        peer = Peer()
        publisher = None
        try:
            with tempfile.TemporaryDirectory() as folder:
                cfg = Path(folder, 'mqtt.json')
                log_path = str(Path(folder, 'control.log'))
                cfg.write_text(json.dumps({'host': '127.0.0.1', 'port': peer.port, 'tls': False,
                                           'control_log': log_path, 'debug_interval_s': 0}))
                publisher = Publisher(cfg, 'testuav01')
                self.assertTrue(publisher.connected.wait(3), 'MQTT CONNACK not received')
                self.assertTrue(publisher.subscribed.wait(3), 'MQTT SUBACK not received')
                self.assertTrue(publisher.publish({'timestamp_ms': 123, 'latitude': None, 'mqtt_connected': True}, {}))
                end = time.monotonic() + 2
                while time.monotonic() < end and not any(t.endswith('/data') for t, _ in peer.messages):
                    time.sleep(.02)
                payload = next(p for t, p in peer.messages if t.endswith('/data'))
                self.assertEqual(payload, '19700101000000123\tTYPE:1\t75:1')
                self.assertEqual(publisher.topic, 'lac/testuav01/data')
                end = time.monotonic() + 2
                while time.monotonic() < end and not publisher.controls_received:
                    time.sleep(.02)
                event = json.loads(Path(log_path).read_text().splitlines()[0])
                self.assertEqual(event['payload'], 'c:setspeed:3;')
                self.assertTrue(event['retain'])
                self.assertFalse(event['executed'])
                publisher.close()
                publisher = None
        finally:
            if publisher:
                publisher.close()
            peer.close()

    def test_tab_encoding_and_unknowns(self):
        values = {'timestamp_ms': 123, 'armed': False, 'battery_remaining': 72,
                  'latitude': None, 'roll': float('nan'), 'longitude': 105.8}
        payload = encode_telemetry(values, 'name')
        self.assertEqual(payload, '19700101000000123\tTYPE:1\tarmed:0\tlongitude:105.8\tbattery_remaining:72')
        self.assertEqual(encode_telemetry(values, 'code'), '19700101000000123\tTYPE:1\t12:0\t26:105.8\t44:72')
        self.assertEqual(encode_telemetry(values), '19700101000000123\tTYPE:1\t12:0\t26:105.8\t44:72')
        self.assertEqual(encode_telemetry({'timestamp_ms': 123, 'uav_id': 'uav01',
                                        'edge_app_version': '2.1.1', 'sequence_no': 0}),
                         '19700101000000123\tTYPE:1\t00:uav01\t01:2.1.1\t03:0')
        for bad in ('x\tarmed:1', 'x:y', 'x\nfoo'):
            with self.assertRaises(ValueError):
                encode_telemetry({'timestamp_ms': 123, 'uav_id': bad})
        with self.assertRaises(ValueError):
            encode_telemetry({'timestamp_ms': None})

    def test_backend_timestamp_type_and_tab_contract(self):
        values = {'timestamp_ms': 1793199524221, 'uav_id': 'uav01',
                  'armed': False, 'battery_remaining': 72, 'latitude': None}
        payload = encode_telemetry(values)
        parts = payload.split('\t')
        self.assertEqual(parts[0], '20261028145844221')
        self.assertEqual(len(parts[0]), 17)
        parsed = datetime.strptime(parts[0], '%Y%m%d%H%M%S%f').replace(tzinfo=timezone.utc)
        self.assertEqual(parsed, datetime(2026, 10, 28, 14, 58, 44, 221000, tzinfo=timezone.utc))
        self.assertEqual(parts[1], 'TYPE:1')
        self.assertEqual(dict(p.split(':') for p in parts[1:]),
                         {'TYPE': '1', '00': 'uav01', '12': '0', '44': '72'})
        self.assertEqual(payload.encode('utf-8').count(b'\x09'), 4)
        self.assertNotIn('\\t', payload)
        self.assertNotIn('\n', payload)
        self.assertEqual(values['timestamp_ms'], 1793199524221)
        self.assertTrue(encode_telemetry({'timestamp_ms': 0}).startswith('19700101000000000\tTYPE:1'))
        self.assertTrue(encode_telemetry({'timestamp_ms': 999}).startswith('19700101000000999\tTYPE:1'))
        self.assertTrue(encode_telemetry({'timestamp_ms': 1000}).startswith('19700101000001000\tTYPE:1'))

    def test_alphanumeric_device_id(self):
        for good in ('uav01', 'UAVLAB02', 'abc123', '7'):
            self.assertEqual(validate_device_id(good), good)
        for bad in ('192.168.50.133', 'uav-01', 'uav_01', 'uav/01', 'uav+01', 'uav\t01', '', 'x'*65):
            with self.assertRaises(ValueError):
                validate_device_id(bad)

    def test_drop_when_offline_or_send_pending(self):
        publisher = Publisher.__new__(Publisher)
        publisher.connected = threading.Event()
        publisher.dropped, publisher.submitted = 0, 0
        self.assertFalse(publisher.publish({}, {}))
        publisher.connected.set()
        publisher.previous = type('Pending', (), {'is_published': lambda self: False})()
        self.assertFalse(publisher.publish({}, {}))
        self.assertEqual(publisher.dropped, 2)


if __name__ == '__main__':
    unittest.main()
