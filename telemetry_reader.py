#!/usr/bin/env python3
"""Read PX4 telemetry via MAVLink Router; no flight commands or PARAM_SET."""
import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from lac_protocol import validate_device_id

VERSION = "2.1.1"
ROOT = Path(__file__).resolve().parent


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def px4_mode(custom):
    main, sub = (custom >> 16) & 255, (custom >> 24) & 255
    if main == 4:
        return {3: 2, 4: 3, 5: 4, 6: 5}.get(sub, 255)
    return {1: 0, 3: 1, 6: 6, 7: 7}.get(main, 255)


def decode_parameter(msg):
    """PX4 legacy PARAM_VALUE uses bytewise encoding for integer parameters."""
    kind, value = int(msg.param_type), float(msg.param_value)
    formats = {1: 'B', 2: 'b', 3: 'H', 4: 'h', 5: 'I', 6: 'i'}
    if kind in formats:
        fmt = formats[kind]
        return struct.unpack('<' + fmt, struct.pack('<f', value)[:struct.calcsize(fmt)])[0]
    if kind == 9 and math.isfinite(value):
        return value
    return None


class Reader:
    def __init__(self, fields, system_id=1, component_id=1, battery_id=0):
        self.fields = fields
        self.system_id, self.component_id = system_id, component_id
        self.battery_id = battery_id
        self.cache, self.parameters, self.parameter_indices = {}, {}, set()
        self.parameter_count = None
        self.bad_data = 0

    def ingest(self, msg, now):
        name = msg.get_type()
        if name == 'BAD_DATA':
            self.bad_data += 1
            return
        if (msg.get_srcSystem(), msg.get_srcComponent()) != (self.system_id, self.component_id):
            return
        if name == 'HEARTBEAT' and int(msg.autopilot) != 12:
            return
        if name == 'BATTERY_STATUS' and int(msg.id) != self.battery_id:
            return
        self.cache[name] = (now, msg)
        if name == 'PARAM_VALUE':
            key = msg.param_id
            if isinstance(key, bytes):
                key = key.decode('ascii', errors='replace')
            key = key.rstrip('\x00')
            self.parameters[key] = {'value': decode_parameter(msg), 'mav_param_type': int(msg.param_type)}
            count = int(msg.param_count)
            if self.parameter_count != count:
                self.parameter_indices.clear()
            self.parameter_count = count
            index = int(msg.param_index)
            if 0 <= index < count:
                self.parameter_indices.add(index)

    def get(self, name, now, max_age=3):
        item = self.cache.get(name)
        return item[1] if item and 0 <= now - item[0] <= max_age else None

    def snapshot(self, now, uav_id, sequence):
        v = {field['name']: None for field in self.fields}
        v.update(uav_id=uav_id, edge_app_version=VERSION,
                 timestamp_ms=time.time_ns() // 1000000, sequence_no=sequence)
        hb_record = self.cache.get('HEARTBEAT')
        v['px4_heartbeat_age'] = max(0, int((now - hb_record[0]) * 1000)) if hb_record else None
        hb = self.get('HEARTBEAT', now)
        v['px4_connected'] = hb is not None
        if hb:
            v['armed'] = bool(int(hb.base_mode) & 128)
            v['flight_mode'] = px4_mode(int(hb.custom_mode))
            v['vehicle_status'] = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 4}.get(int(hb.system_status), 0)
        land = self.get('EXTENDED_SYS_STATE', now)
        if land:
            v['landed_state'] = int(land.landed_state) if 0 <= land.landed_state <= 4 else 0
        pos = self.get('GLOBAL_POSITION_INT', now)
        est = self.get('ESTIMATOR_STATUS', now)
        # Conservative validity for this table's GLOBAL latitude/longitude/altitude.
        # Require absolute horizontal position, vertical position and no GPS glitch/accel error.
        if not hb:
            v['position_valid'] = False
        elif pos and est:
            flags = int(est.flags)
            v['position_valid'] = bool(flags & 16 and flags & 32 and not flags & (1024 | 2048))
        if pos and -900000000 <= pos.lat <= 900000000 and -1800000000 <= pos.lon <= 1800000000:
            v.update(latitude=pos.lat / 1e7, longitude=pos.lon / 1e7,
                     altitude_msl=pos.alt / 1000, altitude_relative=pos.relative_alt / 1000,
                     ground_speed=math.hypot(pos.vx, pos.vy) / 100, vertical_speed=-pos.vz / 100)
        hud = self.get('VFR_HUD', now)
        if hud and finite(hud.groundspeed) and hud.groundspeed >= 0:
            v['ground_speed'] = float(hud.groundspeed)
        att = self.get('ATTITUDE', now)
        if att and all(finite(getattr(att, k)) for k in ('roll', 'pitch', 'yaw')):
            v.update(roll=math.degrees(att.roll), pitch=math.degrees(att.pitch), yaw=math.degrees(att.yaw) % 360)
        altitude = self.get('ALTITUDE', now)
        if altitude and finite(altitude.bottom_clearance) and altitude.bottom_clearance >= 0:
            v['altitude_agl'] = float(altitude.bottom_clearance)
        # HOME_POSITION is event/request based. Clear it after FC disconnect and request anew.
        home = self.get('HOME_POSITION', now, 30)
        if hb and home and -900000000 <= home.latitude <= 900000000 and -1800000000 <= home.longitude <= 1800000000:
            v.update(home_latitude=home.latitude / 1e7, home_longitude=home.longitude / 1e7,
                     home_altitude_msl=home.altitude / 1000)
        # A HOME_POSITION packet alone does not establish home_position_valid.
        bat = self.get('BATTERY_STATUS', now, 10)
        if bat:
            if 0 <= bat.battery_remaining <= 100:
                v['battery_remaining'] = int(bat.battery_remaining)
            cells = list(bat.voltages)
            if cells and cells[0] != 65535:
                total = sum(x for x in cells if 0 < x < 65535)
                total += sum(x for x in getattr(bat, 'voltages_ext', []) if 0 < x < 65535)
                if total:
                    v['battery_voltage'] = total / 1000
            if bat.current_battery != -1:
                v['battery_current'] = bat.current_battery / 100
            # MAV_BATTERY_CHARGE_STATE: 0 undefined, 1 OK, 2 low, ... 5 failed.
            v['battery_warning'] = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}.get(getattr(bat, 'charge_state', 0))
        gps = self.get('GPS_RAW_INT', now, 5)
        if gps:
            v['gps_fix_type'] = int(gps.fix_type) if 0 <= gps.fix_type <= 6 else None
            v['satellites_visible'] = int(gps.satellites_visible) if gps.satellites_visible != 255 else None
        # GPS_RAW_INT eph/epv may be PX4 metric accuracy, not DOP: do NOT guess HDOP/VDOP.
        dop = self.get('GPS_INPUT', now, 5)
        if dop:
            for name, ignore_bit in [('hdop', 2), ('vdop', 4)]:
                val = getattr(dop, name)
                if not dop.ignore_flags & ignore_bit and finite(val) and val >= 0:
                    v[name] = float(val)
        return v


class PiMetrics:
    def __init__(self):
        self.previous_cpu = None

    def sample(self):
        out = {}
        try:
            parts = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
            total, idle = sum(parts[:8]), parts[3] + parts[4]
            if self.previous_cpu:
                dt, di = total - self.previous_cpu[0], idle - self.previous_cpu[1]
                if dt > 0:
                    out['cc_cpu_usage'] = round(max(0, min(100, 100 * (dt - di) / dt)), 2)
            self.previous_cpu = total, idle
            mem = {k: int(val.split()[0]) for k, val in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
            out['cc_memory_usage'] = round(100 * (1 - mem['MemAvailable'] / mem['MemTotal']), 2)
            out['cc_uptime'] = int(float(Path('/proc/uptime').read_text().split()[0]))
            out['cc_disk_free'] = shutil.disk_usage('/').free // 1000000
            temp = Path('/sys/class/thermal/thermal_zone0/temp')
            if temp.exists():
                out['cc_cpu_temperature'] = float(temp.read_text()) / 1000
        except (OSError, ValueError, KeyError, IndexError):
            pass
        # Determine interface used for default IPv4 route. Unknown stays null.
        try:
            result = subprocess.run(['ip', '-j', 'route', 'show', 'default'], capture_output=True, text=True, timeout=1)
            routes = json.loads(result.stdout)
            if routes:
                iface = min(routes, key=lambda x: x.get('metric', 0)).get('dev', '')
                if Path('/sys/class/net', iface, 'wireless').exists():
                    out['connection_type'] = 2
                    if shutil.which('iw'):
                        link = subprocess.run(['iw', 'dev', iface, 'link'], capture_output=True, text=True, timeout=1)
                        for line in link.stdout.splitlines():
                            if line.strip().startswith('signal:'):
                                out['signal_rssi'] = int(float(line.split()[1]))
                elif iface.startswith(('eth', 'en')):
                    out['connection_type'] = 1
                # USB modem interfaces do not prove 4G vs 5G: leave unknown.
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return out


def write_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection', default='udpin:127.0.0.1:14540')
    parser.add_argument('--system-id', type=int, default=1)
    parser.add_argument('--component-id', type=int, default=1)
    parser.add_argument('--battery-id', type=int, default=0)
    parser.add_argument('--uav-id', required=True)
    parser.add_argument('--output', default='reports/status.json')
    parser.add_argument('--jsonl', help='Optional append-only history; grows until stopped')
    parser.add_argument('--read-params', action='store_true', help='Request configuration parameters without changing them')
    parser.add_argument('--request-streams', action='store_true', help='Request telemetry intervals; does not send flight commands')
    parser.add_argument('--duration', type=float, default=0, help='Seconds; 0 runs until Ctrl+C')
    parser.add_argument('--mqtt-config', help='LAC broker config; control messages are logged only, never executed')
    parser.add_argument('--quiet', action='store_true', help='Do not print full JSON every second')
    args = parser.parse_args()
    if args.duration < 0 or not 1 <= args.system_id <= 255 or not 1 <= args.component_id <= 255:
        parser.error('Invalid duration or MAVLink identity')
    try:
        validate_device_id(args.uav_id)
    except ValueError as error:
        parser.error(str(error))
    os.environ.setdefault('MAVLINK20', '1')
    from pymavlink import mavutil
    fields = json.loads((ROOT / 'fields.json').read_text(encoding='utf-8'))
    publisher = None
    if args.mqtt_config:
        from mqtt_publisher import Publisher
        publisher = Publisher(args.mqtt_config, args.uav_id)
    reader = Reader(fields, args.system_id, args.component_id, args.battery_id)
    metrics = PiMetrics()
    # Companion identity distinct from autopilot 1/1 and QGC 255/190.
    conn = mavutil.mavlink_connection(args.connection, source_system=245, source_component=191, dialect='common')
    start = time.monotonic()
    next_snapshot, next_heartbeat, next_home_request = start, start, start
    requested = False
    sequence = 0
    history = None
    if args.jsonl:
        Path(args.jsonl).parent.mkdir(parents=True, exist_ok=True)
        history = open(args.jsonl, 'a', encoding='utf-8')
    print('Reader running. No ARM / TAKEOFF / LAND / OFFBOARD / PARAM_SET.', file=sys.stderr)
    try:
        while not args.duration or time.monotonic() - start < args.duration:
            now = time.monotonic()
            if now >= next_heartbeat:
                conn.mav.heartbeat_send(18, 8, 0, 0, 4)  # onboard controller, invalid autopilot
                next_heartbeat = now + 1
            msg = conn.recv_match(blocking=True, timeout=0.05)
            if msg is not None:
                reader.ingest(msg, time.monotonic())
            now = time.monotonic()
            hb = reader.get('HEARTBEAT', now)
            if not hb:
                requested = False
            elif not requested:
                if args.read_params:
                    reader.parameters.clear()
                    reader.parameter_indices.clear()
                    reader.parameter_count = None
                    conn.mav.param_request_list_send(args.system_id, args.component_id)
                if args.request_streams:
                    # Request-only MAV_CMD_SET_MESSAGE_INTERVAL; log ACKs below.
                    for message_id, hz in [(1, 1), (24, 2), (30, 10), (33, 5), (74, 2), (147, 1), (230, 2), (241, 2), (245, 2)]:
                        conn.mav.command_long_send(args.system_id, args.component_id, 511, 0,
                                                   message_id, 1000000 / hz, 0, 0, 0, 0, 0)
                requested = True
            if hb and args.request_streams and now >= next_home_request:
                conn.mav.command_long_send(args.system_id, args.component_id, 512, 0, 242, 0, 0, 0, 0, 0, 0)
                next_home_request = now + 10
            if msg is not None and msg.get_type() == 'COMMAND_ACK' and (msg.get_srcSystem(), msg.get_srcComponent()) == (args.system_id, args.component_id):
                if msg.command in (511, 512):
                    print(f'Read request ACK command={msg.command} result={msg.result}', file=sys.stderr)
            if now >= next_snapshot:
                values = reader.snapshot(now, args.uav_id, sequence)
                values.update(metrics.sample())
                if publisher:
                    values['mqtt_connected'] = publisher.connected.is_set()
                unknown = [k for k, value in values.items() if value is None]
                report = {'telemetry': values, 'diagnostics': {
                    'unknown_fields': unknown,
                    'message_age_ms': {k: max(0, int((now - stamp) * 1000)) for k, (stamp, _) in reader.cache.items()},
                    'source_system': args.system_id, 'source_component': args.component_id,
                    'battery_id': args.battery_id, 'bad_data_frames': reader.bad_data,
                    'parameter_received': len(reader.parameter_indices), 'parameter_expected': reader.parameter_count,
                    'parameter_download_complete': reader.parameter_count is not None and reader.parameter_count > 0 and len(reader.parameter_indices) == reader.parameter_count,
                    'note': 'Null means unknown, stale, unavailable, or not implemented; not zero or healthy.'}}
                report['diagnostics']['metrics_host'] = os.uname().nodename
                report['diagnostics']['metrics_device'] = 'raspberry_pi' if Path('/proc/device-tree/model').exists() and 'raspberry pi' in Path('/proc/device-tree/model').read_text(errors='replace').lower() else 'other_linux_host'
                if publisher:
                    report['diagnostics']['mqtt'] = {'submitted': publisher.submitted, 'dropped_snapshots': publisher.dropped}
                if args.read_params:
                    report['px4_parameters'] = reader.parameters
                mission = reader.get('MISSION_CURRENT', now)
                if mission:
                    report['diagnostics']['raw_mission'] = {
                        key: int(getattr(mission, key)) for key in ('seq', 'total', 'mission_state', 'mission_mode')
                        if hasattr(mission, key)}
                sys_status = reader.get('SYS_STATUS', now)
                if sys_status:
                    report['diagnostics']['enabled_sensor_error_bits'] = int(sys_status.onboard_control_sensors_enabled) & ~int(sys_status.onboard_control_sensors_health)
                if publisher:
                    publisher.publish(values, report['diagnostics'])
                write_atomic(args.output, report)
                if history:
                    history.write(json.dumps(report, ensure_ascii=False, allow_nan=False) + '\n')
                    history.flush()
                if not args.quiet:
                    print(json.dumps(values, ensure_ascii=False, allow_nan=False), flush=True)
                sequence = (sequence + 1) % (2 ** 32)
                next_snapshot = now + 1
    except KeyboardInterrupt:
        pass
    finally:
        if history:
            history.close()
        conn.close()
        if publisher:
            publisher.close()


if __name__ == '__main__':
    main()
