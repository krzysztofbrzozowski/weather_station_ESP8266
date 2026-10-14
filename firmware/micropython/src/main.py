# Author: Krzysztof Brzozowski
"""One bounded measurement cycle, with REPL access after a manual reset."""
import machine
import time
import gc


def do_connect(wlan, ssid, password, feed, timeout_ms=8000):
    if not ssid:
        wlan.active(False)
        return False
    if wlan.isconnected():
        return True
    wlan.active(True)
    wlan.connect(ssid, password)
    started = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), started) < timeout_ms:
        feed()
        if wlan.isconnected():
            return True
        time.sleep_ms(200)
    return wlan.isconnected()


def do_connect_profiles(wlan, profiles, feed, timeout_ms=8000):
    from station_wifi import connect_profiles
    return connect_profiles(wlan, profiles, feed, do_connect, time, timeout_ms)


def get_measurement(sensor, feed):
    readings = {'temperature': [], 'humidity': [], 'pressure': []}
    bounds = {'temperature': (-40, 85), 'humidity': (0, 100), 'pressure': (300, 1100)}
    for index in range(4):
        feed()
        values = sensor.values
        if index:
            for key in readings:
                value = values[key]
                if not bounds[key][0] <= value <= bounds[key][1]:
                    raise ValueError('Invalid sensor reading: ' + key)
                readings[key].append(value)
        time.sleep_ms(100)
    return {key: round(sum(values) / len(values), 2)
            for key, values in readings.items()}


def run_cycle(config, feed):
    gc.collect()
    from APIrequests import APIRequests
    gc.collect()
    from backup import Backup
    gc.collect()
    from station_clock import StationClock
    from ubinascii import hexlify
    import network

    if not machine.Pin(13, machine.Pin.IN, machine.Pin.PULL_UP).value():
        print('Charging input GPIO13 low: measurement cycle skipped')
        return
    clock = StationClock()
    wlan = network.WLAN(network.STA_IF)
    api = APIRequests(config['API_url'], config['AUTH_TOKEN'], config['HTTP_TIMEOUT_S'])
    queue = None
    try:
        queue = Backup(config['BACKUP_FOLDER'], config['API_url'], config['AUTH_TOKEN'],
                       max_records=config['BACKUP_MAX_RECORDS'],
                       min_free_bytes=config['BACKUP_MIN_FREE_BYTES'],
                       timeout=config['HTTP_TIMEOUT_S'], clock=clock, feed=feed)
    except Exception as error:
        print('Backup storage unavailable:', error)

    data = capture = None
    queued = False
    try:
        feed()
        from BME280 import BME280
        sensor = BME280(i2c=machine.I2C(scl=machine.Pin(5), sda=machine.Pin(4), freq=10000))
        measurement = get_measurement(sensor, feed)
        measurement.update({'mac_address': hexlify(wlan.config('mac'), ':').decode(),
                            'adc': round(machine.ADC(0).read() * 4.2 / 1024, 2)})
        del sensor
        capture = clock.capture()
        data = measurement
        if queue is not None:
            queue.write_backup(data, capture)
            queued = True
            print('Measurement queued')
    except Exception as error:
        print('Measurement or backup write failed:', error)

    feed()
    gc.collect()
    print('Wi-Fi connection starting')
    connected = (do_connect_profiles(wlan, config['WIFI_NETWORKS'], feed, config.get('WIFI_TIMEOUT_S', 8) * 1000)
                 if 'WIFI_NETWORKS' in config else
                 do_connect(wlan, config['SSID'], config['PASSWD'], feed, config.get('WIFI_TIMEOUT_S', 8) * 1000))
    if not connected:
        print('Wi-Fi unavailable; queued measurements retained')
        return
    print('Wi-Fi connected')
    try:
        feed()
        clock.synchronize(api.get_server_time())
        print('Time sync OK')
    except Exception as error:
        print('Time sync failed:', error)
    # If storage fails, still attempt delivery of the current valid measurement.
    if data is not None and capture is not None and not queued:
        record = {'data': data, 'clock': capture['clock'], 'captured': capture['captured']}
        if capture.get('timestamp'):
            data['timestamp'] = capture['timestamp']
        if clock.stamp(record):
            feed()
            api.post(data)
            print('sent to server (storage unavailable)')
    if queue is not None:
        deadline = time.ticks_add(time.ticks_ms(), 20000)
        queue.send_backup(limit=config['BACKUP_MAX_SEND'], feed=feed, deadline_ms=deadline)


def deep_sleep(milliseconds):
    print('Deep sleep:', milliseconds, 'ms')
    try:
        rtc = machine.RTC()
        rtc.irq(trigger=rtc.ALARM0, wake=machine.DEEPSLEEP)
        rtc.alarm(rtc.ALARM0, milliseconds)
    except Exception:
        machine.deepsleep(milliseconds)
        return
    machine.deepsleep()


def main():
    print('Reset cause:', machine.reset_cause())
    started = time.ticks_ms()
    interval_ms = 300000
    interrupted = False
    try:
        watchdog = machine.WDT()
        from settings import load_settings
        config = load_settings()
        interval_ms = config['MEASUREMENT_INTERVAL_S'] * 1000
        if machine.reset_cause() == machine.WDT_RESET:
            print('Watchdog reset: sleeping before retry to avoid a reset loop')
            return
        run_cycle(config, watchdog.feed)
    except KeyboardInterrupt:
        # WDT cannot be disabled on ESP8266. Reset to offer the service window
        # before enabling it again instead of leaving an unstable REPL session.
        interrupted = True
        print('Cycle interrupted; resetting. Press Ctrl+C in the service window.')
    except Exception as error:
        print('Measurement cycle failed:', error)
    finally:
        if interrupted:
            machine.reset()
        else:
            elapsed = time.ticks_diff(time.ticks_ms(), started)
            deep_sleep(max(1000, interval_ms - elapsed))


def start():
    # Timed wakes must stay short. Manual/power/software resets offer a window
    # before main() enables the hardware watchdog or starts the sleep cycle.
    if machine.reset_cause() not in (machine.DEEPSLEEP_RESET, machine.WDT_RESET):
        try:
            print('Service window: press Ctrl+C within 3 s to enter REPL')
            time.sleep_ms(3000)
        except KeyboardInterrupt:
            print('REPL service mode; Ctrl+D restarts the firmware')
            return
    main()


if __name__ == '__main__':
    start()
