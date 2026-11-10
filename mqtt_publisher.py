"""LAC telemetry and control observation. No flight command execution."""
import json
import logging
import math
import os
import ssl
import sys
import threading
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler
from lac_protocol import encode_telemetry, validate_device_id


class Publisher:
    def __init__(self, config_path, uav_id):
        import paho.mqtt.client as mqtt
        cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
        validate_device_id(uav_id)
        host = os.environ.get('MQTT_HOST', cfg.get('host', '')).strip()
        if not host or host == 'CHANGE_ME':
            raise ValueError('Set host in mqtt.local.json or MQTT_HOST to your broker address')
        prefix = cfg.get('topic_prefix', 'lac').strip('/')
        if not prefix or any(c in prefix + uav_id for c in '#+\t\r\n\x00') or any(c in uav_id for c in '/:,;'):
            raise ValueError('MQTT topic/id cannot contain wildcards; uav-id cannot contain slash')
        self.topic = f'{prefix}/{uav_id}/data'
        self.control_topic = f'{prefix}/{uav_id}/control'
        self.debug_topic = f'{prefix}/{uav_id}/debug'
        self.key_mode = cfg.get('parameter_keys', 'code')
        if self.key_mode not in ('name', 'code'):
            raise ValueError('parameter_keys must be name or code')
        self.subscribed = threading.Event()
        self.controls_received = 0
        self.debug_interval = float(cfg.get('debug_interval_s', 10))
        if not math.isfinite(self.debug_interval) or self.debug_interval < 0:
            raise ValueError('debug_interval_s must be finite and nonnegative')
        self.last_debug = 0.0
        control_log = Path(cfg.get('control_log', 'reports/mqtt_control.log'))
        control_log.parent.mkdir(parents=True, exist_ok=True)
        self.control_logger = logging.Logger(f'lac-control-{id(self)}', logging.INFO)
        self.control_handler = RotatingFileHandler(control_log, maxBytes=1_048_576, backupCount=2, encoding='utf-8')
        self.control_logger.addHandler(self.control_handler)
        self.connected = threading.Event()
        self.previous = None
        self.dropped = 0
        self.submitted = 0
        self.session_id = f'{time.time_ns():x}'
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f'lac-{uav_id}-gateway', protocol=mqtt.MQTTv311)
        self.client.max_queued_messages_set(4)
        self.client.max_inflight_messages_set(1)
        self.client.reconnect_delay_set(1, 30)
        self.client.connect_timeout = 3
        username = os.environ.get('MQTT_USERNAME', cfg.get('username'))
        password = os.environ.get(cfg.get('password_env', 'MQTT_PASSWORD'))
        if username:
            self.client.username_pw_set(username, password)
        tls = cfg.get('tls', False)
        if not isinstance(tls, bool):
            raise ValueError('tls must be JSON true or false')
        if tls:
            self.client.tls_set(ca_certs=cfg.get('ca_file') or None,
                                certfile=cfg.get('cert_file') or None,
                                keyfile=cfg.get('key_file') or None,
                                tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.will_set(self.debug_topic, json.dumps({'event': 'connection_lost',
            'session_id': self.session_id, 'control_mode': 'monitor_only'}), qos=1, retain=False)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.host, self.port = host, int(os.environ.get('MQTT_PORT', cfg.get('port', 8883 if tls else 1883)))
        self.client.connect_async(self.host, self.port, keepalive=15)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(self.control_topic, qos=1)
            self.connected.set()
            print(f'MQTT connected. data={self.topic}; control={self.control_topic}; CONTROL MONITOR ONLY', file=sys.stderr, flush=True)
        else:
            self.connected.clear()
            print(f'MQTT connection refused: {reason_code}', file=sys.stderr, flush=True)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if reason_codes and all(not code.is_failure for code in reason_codes):
            self.subscribed.set()
            print(f'MQTT subscribed: {self.control_topic}', file=sys.stderr, flush=True)
        else:
            self.subscribed.clear()
            print(f'MQTT subscription refused: {reason_codes}', file=sys.stderr, flush=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        self.subscribed.clear()

    def _on_message(self, client, userdata, message):
        if message.topic != self.control_topic:
            return
        self.controls_received += 1
        event = {'timestamp_ms': time.time_ns() // 1_000_000, 'topic': message.topic,
                 'retain': bool(message.retain), 'qos': message.qos,
                 'executed': False, 'mode': 'monitor_only'}
        if len(message.payload) > 4096:
            event['error'] = 'payload_exceeds_4096_bytes'
        else:
            try:
                event['payload'] = message.payload.decode('utf-8', errors='strict')
            except UnicodeDecodeError:
                event['error'] = 'invalid_utf8'
        line = json.dumps(event, ensure_ascii=True, allow_nan=False)
        self.control_logger.info(line)
        if self.controls_received <= 10 or self.controls_received % 100 == 0:
            print('MQTT CONTROL (NOT EXECUTED): ' + line, file=sys.stderr, flush=True)

    def publish(self, values, diagnostics):
        """No broker connection or stalled send => drop snapshot rather than queue history."""
        if not self.connected.is_set():
            self.dropped += 1
            return False
        if self.previous is not None and not self.previous.is_published():
            self.dropped += 1
            return False
        payload = encode_telemetry(values, self.key_mode)
        if len(payload.encode('utf-8')) > 131072:
            self.dropped += 1
            return False
        # QoS0 = latest observation. No offline history, no flight commands.
        result = self.client.publish(self.topic, payload, qos=0, retain=False)
        if int(result.rc) != 0:
            self.dropped += 1
            return False
        self.previous = result
        self.submitted += 1
        now = time.monotonic()
        if self.debug_interval and now - self.last_debug >= self.debug_interval:
            debug = {'event': 'telemetry_status', 'timestamp_ms': values['timestamp_ms'],
                     'session_id': self.session_id, 'control_mode': 'monitor_only',
                     'control_subscribed': self.subscribed.is_set(),
                     'controls_received': self.controls_received,
                     'parameter_keys': self.key_mode, 'diagnostics': diagnostics}
            self.client.publish(self.debug_topic, json.dumps(debug, ensure_ascii=True, allow_nan=False), qos=0, retain=False)
            self.last_debug = now
        return True

    def close(self):
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self.control_logger.removeHandler(self.control_handler)
            self.control_handler.close()

