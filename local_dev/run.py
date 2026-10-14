"""Run the firmware on CPython with simulated hardware and HTTP responses."""

import argparse
import binascii
import json
import os
from pathlib import Path
import runpy
import sys
import struct
import time
from unittest.mock import patch

from stubs import BME280, machine, network, urequests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Simulate unavailable Wi-Fi")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(__file__).resolve().parent / ".state",
        help="Directory for simulated device files, including backup_logs",
    )
    args = parser.parse_args()
    print("[local] SIMULATION: Wi-Fi and HTTP are mocked; no readings are sent to the server.")
    firmware = Path(__file__).resolve().parents[1] / "firmware" / "src"
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    network.offline = args.offline
    network.WLAN.connected = False
    network.WLAN.enabled = False
    machine.configure_state(state_dir)
    elapsed_ms = [0]
    def sleep_ms(milliseconds):
        elapsed_ms[0] += milliseconds
    modules = {
        "machine": machine,
        "network": network,
        "BME280": BME280,
        "urequests": urequests,
        "requests": urequests,
        "ujson": json,
        "ubinascii": binascii,
        "ustruct": struct,
    }
    original_directory = Path.cwd()
    sys.path.insert(0, str(firmware))
    try:
        os.chdir(state_dir)
        with patch.dict(sys.modules, modules), patch.object(
            time, "sleep_ms", sleep_ms, create=True
        ), patch.object(time, "ticks_ms", lambda: elapsed_ms[0], create=True), patch.object(
            time, "ticks_diff", lambda a, b: a - b, create=True
        ), patch.object(time, "ticks_add", lambda a, b: a + b, create=True), patch.object(
            time, "time", machine.rtc_time
        ):
            runpy.run_path(str(firmware / "boot.py"), run_name="__boot__")
            runpy.run_path(str(firmware / "main.py"), run_name="__main__")
    except machine.DeepSleep as sleep:
        print("[local] Deep sleep: {} ms; simulation finished".format(sleep.milliseconds))
    finally:
        os.chdir(original_directory)
        sys.path.remove(str(firmware))


if __name__ == "__main__":
    main()
