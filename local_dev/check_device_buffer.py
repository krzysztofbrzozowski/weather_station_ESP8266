"""Run with mpremote run: check real device storage without sending test data."""
import gc
import os
from ubinascii import hexlify
from backup import Backup
from settings import load_settings
from station_clock import StationClock

gc.collect()
config = load_settings()
folder = '_buffer_check_' + hexlify(os.urandom(4)).decode()
os.mkdir(folder)  # Do not touch an existing directory if the name collides.
try:
    queue = Backup(folder, config['API_url'], config['AUTH_TOKEN'], max_records=3,
                   min_free_bytes=config['BACKUP_MIN_FREE_BYTES'], clock=StationClock())
    data = {'temperature': 20, 'humidity': 40, 'pressure': 1013,
            'mac_address': '02:00:00:00:00:01'}
    for index in range(4):
        data['temperature'] = 20 + index
        queue.write_backup(data)
    names = queue._files()
    assert len(names) == 3
    assert [queue._read(name)['data']['temperature'] for name in names] == [21, 22, 23]
    print('PASS: circular buffer retains newest records on real flash')
    with open(folder + '/' + names[-1]) as file:
        raw = file.read()
    with open(folder + '/q0000000005.json.tmp', 'w') as file:
        file.write(raw)
    queue = Backup(folder, config['API_url'], config['AUTH_TOKEN'], max_records=3)
    assert queue._files()[-1] == 'q0000000005.json'
    assert len(queue._files()) == 3
    print('PASS: complete temporary write recovered')
    with open(folder + '/q0000000006.json.tmp', 'w') as file:
        file.write('{"version":')
    queue = Backup(folder, config['API_url'], config['AUTH_TOKEN'], max_records=3)
    assert len(queue._files()) == 3
    assert not [name for name in os.listdir(folder) if name.endswith('.tmp')]
    print('PASS: incomplete write discarded without losing committed records')
finally:
    for name in os.listdir(folder):
        os.remove(folder + '/' + name)
    os.rmdir(folder)
    print('Temporary test files removed; production buffer untouched')
