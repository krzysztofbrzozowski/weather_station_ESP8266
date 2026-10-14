"""Read the station's Python configuration next to this module."""


def load_settings(path=None):
    if path is None:
        directory = __file__.rsplit('/', 1)[0] if '/' in __file__ else '.'
        path = directory + '/.settings'

    values = {}
    try:
        with open(path) as config_file:
            exec(config_file.read(), values)
    except OSError:
        raise OSError('Cannot read settings file: ' + path)

    required = ('API_url', 'AUTH_TOKEN', 'BACKUP_FOLDER', 'SSID', 'PASSWD')
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
    }
    result = {key: values[key] for key in required}
    for key, limits in defaults.items():
        value = values.get(key, limits[0])
        if type(value) is not int or not limits[1] <= value <= limits[2]:
            raise ValueError('Invalid numeric setting: ' + key)
        result[key] = value
    folder = result['BACKUP_FOLDER']
    if not folder or folder in ('.', '..') or '/' in folder or '\\' in folder:
        raise ValueError('BACKUP_FOLDER must be a single directory name')
    return result
