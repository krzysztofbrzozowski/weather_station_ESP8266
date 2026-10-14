"""Read /.settings on the device or firmware/.settings on CPython."""
import sys


def normalize_mac(value):
    if not isinstance(value, str) or len(value) != 17:
        raise ValueError('Invalid MAC in AUTH_TOKENS')
    value = value.lower().replace('-', ':')
    for i, char in enumerate(value):
        if (i % 3 == 2 and char != ':') or (i % 3 != 2 and char not in '0123456789abcdef'):
            raise ValueError('Invalid MAC in AUTH_TOKENS')
    return value


def valid_token(value):
    return isinstance(value, str) and 1 <= len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value)


def network_profiles(values):
    profiles = values.get('WIFI_NETWORKS') if 'WIFI_NETWORKS' in values else [
        {'SSID': values.get('SSID'), 'PASSWD': values.get('PASSWD')}]
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 4:
        raise ValueError('WIFI_NETWORKS must contain 1-4 SSID/PASSWD profiles')
    seen = set()
    for profile in profiles:
        if (not isinstance(profile, dict) or set(profile) != {'SSID', 'PASSWD'} or
            not isinstance(profile['SSID'], str) or not isinstance(profile['PASSWD'], str) or
            not 1 <= len(profile['SSID'].encode()) <= 32 or len(profile['PASSWD'].encode()) > 64 or
            '\0' in profile['SSID'] or '\0' in profile['PASSWD']):
            raise ValueError('Invalid Wi-Fi profile; SSID must have 1-32 bytes, PASSWD 0-64 bytes')
        identity = (profile['SSID'], profile['PASSWD'])
        if identity in seen:
            raise ValueError('Duplicate Wi-Fi profile')
        seen.add(identity)
    return profiles


def select_token(values, mac=None):
    if 'AUTH_TOKENS' not in values:
        token = values.get('AUTH_TOKEN')
        if not valid_token(token):
            raise ValueError('Invalid AUTH_TOKEN')
        return token
    mapping = values['AUTH_TOKENS']
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 100:
        raise ValueError('AUTH_TOKENS must contain 1-100 MAC/token entries')
    tokens = {}
    for key, token in mapping.items():
        key = normalize_mac(key)
        if key in tokens or not valid_token(token):
            raise ValueError('Duplicate MAC or invalid token in AUTH_TOKENS')
        tokens[key] = token
    if mac is None:
        import network
        raw = network.WLAN(network.STA_IF).config('mac')
        mac = ':'.join('%02x' % byte for byte in raw)
    mac = normalize_mac(mac)
    token = tokens.get(mac, '')
    if not token:
        print('No API token for MAC {}; uploads disabled; measurements will be queued'.format(mac))
    return token


def load_settings(path=None, mac=None):
    if path is None:
        module_file = globals().get('__file__', '')
        if sys.implementation.name == 'cpython' and module_file:
            # Host layout: firmware/micropython/src/settings.py. Independent
            # of the simulator's working directory and shared with Arduino.
            path = module_file.rsplit('/', 3)[0] + '/.settings'
        else:
            # Deployed files live at the device root; ESP8266 may omit __file__.
            path = '/.settings'

    values = {}
    try:
        with open(path) as config_file:
            exec(config_file.read(), values)
    except OSError:
        raise OSError('Cannot read settings file: ' + path)

    required = ('API_url', 'BACKUP_FOLDER')
    for key in required:
        if key not in values:
            raise ValueError('Missing setting: ' + key)
        if not isinstance(values[key], str):
            raise ValueError('Setting must be a string: ' + key)
    defaults = {
        'MEASUREMENT_INTERVAL_S': (300, 10, 3600),
        'HTTP_TIMEOUT_S': (3, 1, 3),
        'BACKUP_MAX_RECORDS': (144, 1, 512),
        'BACKUP_MIN_FREE_BYTES': (32768, 8192, 1048576),
        'BACKUP_MAX_SEND': (6, 1, 20),
        'WIFI_TIMEOUT_S': (8, 1, 30),
    }
    result = {key: values[key] for key in required}
    profiles = network_profiles(values)
    if 'WIFI_NETWORKS' in values:
        result['WIFI_NETWORKS'] = profiles
    else:
        result.update(profiles[0])
    result['AUTH_TOKEN'] = select_token(values, mac)
    for key, limits in defaults.items():
        value = values.get(key, limits[0])
        if type(value) is not int or not limits[1] <= value <= limits[2]:
            raise ValueError('Invalid numeric setting: ' + key)
        result[key] = value
    folder = result['BACKUP_FOLDER']
    if not folder or folder in ('.', '..') or '/' in folder or '\\' in folder:
        raise ValueError('BACKUP_FOLDER must be a single directory name')
    return result
