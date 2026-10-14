"""Shared simulated Wi-Fi interface; no real network access."""

STA_IF = 0
offline = False


class WLAN:
    enabled = False
    connected = False

    def __init__(self, interface=STA_IF):
        self.interface = interface

    def active(self, value=None):
        if value is not None:
            WLAN.enabled = bool(value)
            if not value:
                WLAN.connected = False
        return WLAN.enabled

    def connect(self, ssid, password):
        WLAN.connected = WLAN.enabled and not offline

    def isconnected(self):
        return WLAN.connected and not offline

    def config(self, key):
        if key == "mac":
            return b"\x02\x00\x00\x00\x00\x01"
        raise ValueError("Unsupported WLAN config: {}".format(key))
