"""LAC TAB telemetry format. Unknown values are omitted, never replaced by zero."""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

FIELDS = json.loads(Path(__file__).with_name('fields.json').read_text(encoding='utf-8'))


def validate_device_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,64}', value):
        raise ValueError('deviceID must be 1..64 ASCII letters/digits, e.g. uav01; no IP dots or separators')
    return value


def encode_telemetry(values, key_mode='code'):
    if key_mode not in ('name', 'code'):
        raise ValueError('parameter_keys must be name or code')
    timestamp = values.get('timestamp_ms')
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError('timestamp_ms must be a nonnegative integer')
    packet_time = datetime.fromtimestamp(timestamp // 1000, timezone.utc)
    parts = [packet_time.strftime('%Y%m%d%H%M%S') + f'{timestamp % 1000:03d}', 'TYPE:1']
    for field in FIELDS:
        name = field['name']
        if name == 'timestamp_ms':
            continue
        value = values.get(name)
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = str(int(value))
        elif isinstance(value, (int, float)):
            if not math.isfinite(value):
                continue
            encoded = str(value)
        elif isinstance(value, str):
            if any(c in value for c in '\t\r\n:,;\x00'):
                raise ValueError(f'Unsupported delimiter in {name}')
            encoded = value
        else:
            raise ValueError(f'Unsupported type for {name}')
        key = name if key_mode == 'name' else field['code']
        parts.append(f'{key}:{encoded}')
    return '\t'.join(parts)
