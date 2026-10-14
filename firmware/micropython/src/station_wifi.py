"""Bounded multi-network discovery and a persistent successful-profile cache."""

def wifi_fingerprint(profiles):
    value = 2166136261
    for profile in profiles:
        for key in ('SSID', 'PASSWD'):
            for byte in profile[key].encode():
                value = ((value ^ byte) * 16777619) & 0xffffffff
            value = (value * 16777619) & 0xffffffff
    return value


def connect_profiles(wlan, profiles, feed, connect_one, time, timeout_ms=8000):
    import ujson
    import os
    fingerprint = wifi_fingerprint(profiles)
    preferred = -1
    try:
        with open('.wifi.json') as file:
            raw = file.read(257)
        if len(raw) > 256:
            raise ValueError('Oversized Wi-Fi preference')
        cache = ujson.loads(raw)
        if (type(cache.get('version')) is int and cache['version'] == 1 and
                cache.get('fingerprint') == fingerprint and type(cache.get('index')) is int and
                0 <= cache['index'] < len(profiles)):
            preferred = cache['index']
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    started = time.ticks_ms()
    budget = min(timeout_ms * len(profiles) + 5000, 45000)
    pending = len(profiles)
    wlan.active(True)

    def attempt(index):
        nonlocal pending
        remaining = budget - time.ticks_diff(time.ticks_ms(), started)
        if remaining <= 0:
            return False
        allowed = min(timeout_ms, remaining // max(1, pending))
        pending = max(0, pending - 1)
        wlan.disconnect()
        profile = profiles[index]
        print('Wi-Fi trying profile {}/{}'.format(index + 1, len(profiles)))
        return connect_one(wlan, profile['SSID'], profile['PASSWD'], feed, allowed)

    selected = -1
    if wlan.isconnected():
        current = wlan.config('essid')
        for index, profile in enumerate(profiles):
            if profile['SSID'] == current:
                selected = index
                break
    if selected < 0 and preferred >= 0 and attempt(preferred):
        selected = preferred
    if selected < 0:
        visible = None
        if len(profiles) > 1:
            print('Wi-Fi scanning configured networks')
            feed()
            try:
                visible = set()
                for row in wlan.scan():
                    try:
                        visible.add(row[0].decode() if isinstance(row[0], bytes) else row[0])
                    except ValueError:
                        pass
            except Exception:
                visible = None  # A failed scan must not prevent direct attempts.
            feed()
        pending = sum(1 for index, profile in enumerate(profiles)
                      if index != preferred and (visible is None or profile['SSID'] in visible))
        for index, profile in enumerate(profiles):
            if index == preferred or (visible is not None and profile['SSID'] not in visible):
                continue
            if attempt(index):
                selected = index
                break
    if selected < 0:
        wlan.disconnect()
        return False
    if selected != preferred:
        cache_path = '.wifi.json'
        try:
            with open(cache_path + '.tmp', 'w') as file:
                file.write(ujson.dumps({'version': 1, 'fingerprint': fingerprint, 'index': selected}))
                file.flush()
            os.rename(cache_path + '.tmp', cache_path)
        except OSError:
            print('Wi-Fi preference could not be saved; next boot will discover again')
    print('Wi-Fi selected profile {}/{}'.format(selected + 1, len(profiles)))
    return True


