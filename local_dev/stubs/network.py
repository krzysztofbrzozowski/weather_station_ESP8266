"""Shared simulated Wi-Fi interface; no real network access."""

STA_IF = 0
offline = False


class WLAN:
    enabled = False
    connected = False
    ssid = ''
    available_networks = None  # Dict SSID -> password; None accepts any network.

    def __init__(self, interface=STA_IF):
        self.interface = interface

    def active(self, value=None):
        if value is not None:
            WLAN.enabled = bool(value)
            if not value:
                WLAN.connected = False
        return WLAN.enabled

    def connect(self, ssid, password):
        WLAN.ssid = ssid
        WLAN.connected = WLAN.enabled and not offline and (WLAN.available_networks is None or
            (ssid in WLAN.available_networks and WLAN.available_networks[ssid] == password))

    def disconnect(self):
        WLAN.connected = False

    def scan(self):
        if offline:
            return []
        if WLAN.available_networks is None:
            raise NotImplementedError('No simulated scan inventory; try configured profiles')
        return [(ssid.encode(), b'\0' * 6, 1, -50, 3, False) for ssid in WLAN.available_networks]

    def isconnected(self):
        return WLAN.connected and not offline

    def config(self, key):
        if key == "mac":
            return b"\x02\x00\x00\x00\x00\x01"
        if key == 'essid':
            return WLAN.ssid
        raise ValueError("Unsupported WLAN config: {}".format(key))
