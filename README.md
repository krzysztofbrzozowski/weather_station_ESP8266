# Weather station based on ESP8266 and BME280
## Repository Contents

- **firmware/micropython/src/** - MicroPython files deployed to the ESP8266.
- **firmware/micropython/build/** - Generated MicroPython deployment files, ignored by Git.
- **firmware/.settings** - Shared private configuration for both firmware implementations.
- **firmware/arduino/** - Native C++ firmware using Arduino, BearSSL HTTPS and LittleFS;
  see its [build and migration guide](firmware/arduino/README.md).
- **local_dev/** - CPython launcher and runtime stubs for local development.
- **platformio.sh** - Build or flash Arduino with current shared settings and image verification.

The board currently runs Arduino. For settings, builds and uploads, use the
[Arduino guide](firmware/arduino/README.md#updating-settings-and-firmware).
The deployment instructions below describe MicroPython.

## Configuration

Both implementations use the shared host configuration `firmware/.settings`.
The local MicroPython launcher reads it directly; deployment copies it to
`/.settings` on the device. Arduino's preparation tool converts it into
`/config.json` for the device. Changing the host file alone does not update the board.
If it is missing, copy `firmware/.settings.example` to that path and edit it:

```python
API_url = 'Your API url'
AUTH_TOKENS = {
    'dc:4f:22:7c:3a:fa': 'Your-token-for-first-device',
    'dc:4f:22:7c:3a:fc': 'Your-token-for-second-device',
}
BACKUP_FOLDER = 'backup_logs'
WIFI_NETWORKS = [
    {'SSID': 'Home-WiFi', 'PASSWD': 'Your-home-password'},
    {'SSID': 'Office-WiFi', 'PASSWD': 'Your-office-password'},
]
```

The file uses Python assignments and is ignored by Git. `API_url` and
`BACKUP_FOLDER` are strings. `WIFI_NETWORKS` holds 1–4 SSID/password profiles;
the legacy `SSID` and `PASSWD` strings still work when the list is absent.
The list takes precedence over legacy credentials. Each SSID is 1–32 UTF-8
bytes, each password 0–64 bytes; an empty password selects an open network.
Exact duplicate profiles are rejected; the same SSID with different passwords
is allowed.

On first use, multi-network firmware scans for configured visible SSIDs and
tries them in list order. It saves the successful profile in `/.wifi.json`,
which survives deep sleep and power loss. Later cycles try that profile first;
if connection fails they scan again and try the other visible profiles. A scan
failure falls back to direct attempts. Only a changed successful preference is
written to flash. Changing credentials/order invalidates the saved preference.
If all networks fail, measurements stay queued and discovery retries next cycle.

`AUTH_TOKENS` maps Wi-Fi station MAC
addresses to their API tokens. Each board selects its token at startup; the same
settings file/image can be used on all boards. MACs are case insensitive and
accept colons or hyphens. Use 1–100 entries and 1–256 printable ASCII characters
per token, without spaces. Duplicate MACs after normalization are rejected.

An unknown MAC leaves uploads disabled while measurements remain queued and the
normal sleep cycle continues. The console reports the missing MAC, without
printing any tokens. The legacy single-device `AUTH_TOKEN = 'Your API token'`
format still works when `AUTH_TOKENS` is absent. A present map takes precedence;
there is no fallback to another device's token.

Optional numeric controls and their defaults are:

```python
MEASUREMENT_INTERVAL_S = 300
HTTP_TIMEOUT_S = 3
BACKUP_MAX_RECORDS = 144
BACKUP_MIN_FREE_BYTES = 32768
BACKUP_MAX_SEND = 6
```

`BACKUP_FOLDER` is a single directory name. Local development uses the same file even when launched from a
different working directory; backup files still go into the simulated device
filesystem. When using `local_dev/run.py`, HTTP and Wi-Fi remain simulated.
Use `local_dev/send_test_measurements.py` below for real HTTP requests.

The measurement interval accepts 10–3600 seconds and the HTTP timeout 1–3 seconds
to stay within the ESP8266's sleep timer and fixed watchdog constraints.

## Local development

Run from the repository root with Python 3 (standard library only):

```sh
python3 local_dev/run.py
```

The launcher executes `firmware/micropython/src/boot.py` and `firmware/micropython/src/main.py`, replacing
hardware and HTTP modules with runtime stubs. It supplies fixed BME280 readings,
an ADC value and a MAC address, simulates Wi-Fi and API responses, skips
millisecond delays, and finishes when the firmware enters deep sleep.
It does not send real HTTP requests or require Wi-Fi credentials.

To simulate unavailable Wi-Fi and exercise measurement backups:

```sh
python3 local_dev/run.py --offline
python3 local_dev/run.py
```

Simulated device files are stored in `local_dev/.state/` (ignored by Git).
The second command simulates sending up to `BACKUP_MAX_SEND` pending readings.
Simulated RTC state is retained in `.state/.rtc.json` between invocations.
Use `--state-dir /path/to/directory` to choose a different device filesystem.
Adjust simulated readings in `local_dev/stubs/` as needed.

These stubs support the interfaces currently used by the application. They
replace the BME280 driver entirely and do not verify I2C communication,
sensor calibration, real API compatibility, or ESP8266 timing and power behavior.

## Deployment

Build the modules from `firmware/micropython/src/` on the computer and upload the resulting
`.mpy` files, `boot.py`, `main.py` and the private `.settings` to the ESP8266 root
as shown below. Configure Wi-Fi and API settings in `.settings`.
The `local_dev/` directory stays on the development
computer and is not deployed to the board.

Use a MicroPython build for your exact ESP8266 board with a working `urequests`
or `requests` module supporting the `timeout` argument. Do not copy the local
stubs to the board. For resilience to interrupted writes, use a LittleFS
filesystem. Connect GPIO16 to RST for timed wakeup, connect BME280 SCL to GPIO5
and SDA to GPIO4, and confirm its I2C address is `0x76`.
The existing GPIO13 charging check and ADC divider scaling are preserved;
they must match your PCB. Test a complete sleep/wake cycle on the actual board.

### 1. Install the flashing tools

The commands below use **macOS/Linux**, Python **3.10+**, and the current
`esptool` command syntax. Run them from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade esptool mpremote
```

`esptool` installs the MicroPython interpreter; `mpremote` transfers this
project's Python files afterwards. See the official
[esptool installation guide](https://docs.espressif.com/projects/esptool/en/latest/esp8266/installation.html).

### 2. Connect the board and identify its flash size

Use a USB data cable and close other serial terminals. List connected ports:

```sh
python -m serial.tools.list_ports
```

Set the actual port shown above (the following macOS name is an example;
Linux commonly uses `/dev/ttyUSB0`):

```sh
ESP_PORT='/dev/cu.usbserial-210'
python -m esptool --chip esp8266 --port "$ESP_PORT" flash-id
```

Read `Detected flash size` before choosing a firmware variant. Boards with an
automatic reset circuit usually enter the bootloader themselves. Otherwise,
hold **FLASH/BOOT** (GPIO0 low), press and release **RST**, and retry the command.
For a bare module, pull GPIO0 to GND while resetting; release GPIO0 for normal boot.

### 3. Download the latest stable MicroPython binary

Open the official [ESP8266 firmware downloads](https://micropython.org/download/ESP8266_GENERIC/)
and choose the newest `.bin` under **Releases** for your flash size. As of
**2026-10-14**, the latest stable release is **v1.29.0 (2026-08-24)**; check that
page again when installing later.

| Detected flash | Firmware choice |
| --- | --- |
| 2–4 MiB | Standard `ESP8266_GENERIC` build |
| 1 MiB | `FLASH_1M` variant; fewer bundled modules and less buffer space |
| 512 KiB | Unsuitable for this project: the current variant has no filesystem |
| 8–16 MiB | Standard build plus the [additional RF calibration procedure](https://docs.micropython.org/en/latest/esp8266/tutorial/intro.html#boards-with-more-than-4mb-of-flash) |

Use a release build for the station. Preview builds are development snapshots.
Rename your downloaded file to `micropython-esp8266.bin` and set its path:

```sh
MICROPYTHON_BIN="$HOME/Downloads/micropython-esp8266.bin"
```

### 4. Erase the old firmware and flash MicroPython

**Erasing removes everything on the board, including `.settings` and unsent
measurements.** Save anything you need before this step. The standard procedure
below covers boards with 1–4 MiB flash:

```sh
python -m esptool --chip esp8266 --port "$ESP_PORT" erase-flash
python -m esptool --chip esp8266 --port "$ESP_PORT" --baud 460800 write-flash --flash-size detect 0x0 "$MICROPYTHON_BIN"
```

The ESP8266 image starts at **`0x0`**. Current esptool uses names such as
`write-flash` and `erase-flash`; older tutorials use underscores.
If writing is unreliable, retry at `--baud 115200`. If flashing succeeds but
MicroPython does not boot, retry with the compatibility flash mode:

```sh
python -m esptool --chip esp8266 --port "$ESP_PORT" --baud 115200 write-flash --flash-size detect --flash-mode dout 0x0 "$MICROPYTHON_BIN"
```

Release FLASH/GPIO0 and press RST after flashing. Let the first boot initialize
the filesystem, then check the interpreter:

```sh
mpremote connect "$ESP_PORT" exec "import sys; print(sys.implementation)"
```

Command references: [MicroPython ESP8266 installation](https://docs.micropython.org/en/latest/esp8266/tutorial/intro.html)
and [esptool flash commands](https://docs.espressif.com/projects/esptool/en/latest/esp8266/esptool/basic-commands.html).

### 5. Prepare settings and the HTTP library

Create `firmware/.settings` from `.settings.example` **only if it does not
already exist**, then enter the actual API and Wi-Fi settings in your editor.
Keep an existing private configuration when updating.

```sh
test -f firmware/.settings || cp firmware/.settings.example firmware/.settings
mpremote connect "$ESP_PORT" mip install requests
mpremote connect "$ESP_PORT" exec "import requests; print('HTTP library available')"
```

Installing `requests` uses the computer's Internet connection and USB transfer;
the board does not need to join Wi-Fi for this step. The firmware supports
`urequests` as well. If an older copy of `urequests.py` was previously uploaded,
update or remove that obsolete copy so it does not shadow the current library.
See [MicroPython package installation](https://docs.micropython.org/en/latest/reference/packages.html#installing-packages-with-mpremote).

### 6. Upload the station code

ESP8266 has limited RAM. Compiling all source modules on the device can fail
with `MemoryError` even when the files fit in flash. Precompile the importable
modules on the computer, using an `mpy-cross` release matching the board's
MicroPython version. For **MicroPython 1.29.0**:

```sh
python -m pip install 'mpy-cross==1.29.0.post2'
python local_dev/build_firmware.py
```

The build goes into `firmware/micropython/build/` (ignored by Git). Entry points `boot.py`
and `main.py` stay as Python source; the other modules become `.mpy` files.
The builder does not copy private settings. See
[MicroPython RAM and precompilation](https://docs.micropython.org/en/latest/reference/constrained.html#compilation-phase)
and [.mpy version compatibility](https://docs.micropython.org/en/latest/reference/mpyfiles.html).

Upload the build and explicitly include the hidden `.settings`:

```sh
mpremote connect "$ESP_PORT" fs cp firmware/micropython/build/APIrequests.mpy firmware/micropython/build/BME280.mpy firmware/micropython/build/backup.mpy firmware/micropython/build/settings.mpy firmware/micropython/build/station_clock.mpy firmware/micropython/build/station_wifi.mpy firmware/micropython/build/boot.py firmware/micropython/build/main.py firmware/.settings :/
```

If the older `.py` modules from this project are already on the device, remove
**those source copies only after their `.mpy` replacements upload successfully**:

```sh
mpremote connect "$ESP_PORT" fs rm :APIrequests.py :BME280.py :backup.py :settings.py :station_clock.py :station_wifi.py
```

Skip that removal command on a clean installation. Keep `main.py`, `boot.py`,
`.settings`, `lib/` and measurement backups. Then verify the installed modules:

```sh
mpremote connect "$ESP_PORT" exec "import APIrequests, BME280, backup, settings, station_clock, station_wifi; settings.load_settings(); print('Firmware imports and settings OK')"
mpremote connect "$ESP_PORT" fs ls
```

The loader supports ESP8266 builds which omit `__file__`: it reads `/.settings`
on the board and the shared `firmware/.settings` during local development.

Do not upload `local_dev/`, its stubs, or `.settings.example`. Confirm the BME280
is visible before starting the application; its default address should be `0x76`:

```sh
mpremote connect "$ESP_PORT" exec "from machine import Pin, I2C; print([hex(a) for a in I2C(scl=Pin(5), sda=Pin(4), freq=10000).scan()])"
```

### 7. Start the station and check one complete cycle

```sh
mpremote connect "$ESP_PORT" repl
```

Press **Ctrl+D** in the REPL to restart MicroPython and run `main.py`. Watch the
measurement/backup messages, then verify that the station wakes for the next
cycle (five minutes by default). Output pauses during deep sleep. GPIO16 must
be connected to RST for timed wakeup. Press **Ctrl+]** to leave the terminal.
For later source updates, repeat the upload step; erasing the flash is only
needed for a clean interpreter installation and would delete pending readings.
See the [mpremote manual](https://docs.micropython.org/en/latest/reference/mpremote.html).

To enter REPL while the station is running, connect the terminal first, press the
physical **RST** button, then press **Ctrl+C** when you see
`Service window: press Ctrl+C within 3 s to enter REPL`. This leaves you at `>>>`
before the hardware watchdog is enabled. **Ctrl+D** starts the firmware again.
Interrupting an active measurement cycle instead requests a hardware reset;
press **Ctrl+C** again during the service window to enter REPL. Timed deep-sleep
wakes and watchdog recovery skip this window. An already sleeping chip cannot
receive Ctrl+C: use RST to wake it before trying to interrupt startup.

This service window requires the updated `main.py` on the device. After building,
upload `firmware/micropython/build/main.py` using the upload instructions above. This does
not require erasing flash or reinstalling MicroPython.

Keep the terminal at **115200 baud** for REPL and application messages. ESP8266
ROM startup messages normally use **74880 baud**, so some unreadable characters
around reset are expected at the REPL speed; see the
[Espressif boot log reference](https://docs.espressif.com/projects/esptool/en/latest/esp8266/advanced-topics/boot-mode-selection.html#boot-log).

For the custom weather-station PCB, close **SJ2** to connect the `WAKE` net (GPIO16) to
`RST`. An open SJ2 prevents timed deep-sleep wake unless another physical
connection provides that path. A restart seen while opening/closing a USB serial
terminal is not proof of timed wake: the adapter can toggle reset control lines.

To check wake without reopening the port, use the host-side diagnostic (with the
current `main.py` installed so it prints the reset cause):

```sh
python local_dev/check_device_sleep.py --port "$ESP_PORT" --seconds 10
```

It keeps the serial connection open throughout sleep and requires a delayed
boot with `machine.DEEPSLEEP_RESET`. If the device is already asleep and cannot
enter the REPL, reset it before starting the diagnostic. A timeout with the
sleep command confirmed means automatic wake was not observed; do not interpret
it as a successful test. This check does not measure sleep current.
To catch a manual reset while keeping USB connected, start it with
`--wait-for-reset 120`, then press RST while the script is waiting.

If the firmware reports **HTTP 401**, register the physical device's MAC address
on the server and use that sensor's token. To read the MAC without showing secrets:

```sh
mpremote connect "$ESP_PORT" exec "import network, ubinascii; print(ubinascii.hexlify(network.WLAN(network.STA_IF).config('mac'), ':').decode())"
```

Rejected authentication leaves measurements in the offline buffer. Once the
MAC/token configuration is corrected, later cycles retry those records.

## Offline buffer and recovery

The station saves each valid measurement **before** attempting HTTP, even while
online. It uses an oldest-first bounded queue of independent JSON files, with
sequence filenames that do not depend on the clock. By default it retains at
most **144 readings** (about 12 hours at five-minute intervals, if space permits).
It removes the oldest queued readings when this limit or the free-space reserve
is reached. It only evicts recognized measurement files in `BACKUP_FOLDER`.
If other files consume all available space, the station stops writing backups
and still attempts to send the current reading when Wi-Fi is available.

Each update is written to a temporary file, closed, synced and renamed. On boot,
complete interrupted writes are recovered and incomplete temporary files are
discarded. A corrupt record does not block the remaining queue. This depends on
the underlying filesystem and does not replace hardware power-loss testing.
JSON keeps records inspectable and independently recoverable; pickle adds no
useful benefit for this small, fixed measurement payload. Actual disk usage
depends on filesystem allocation blocks, so eviction may occur before 144 files.

The timestamp is captured at measurement time and persisted before the first
POST. Retries send the same payload and timestamp; the API must accept identical
replays idempotently. HTTP errors, timeouts and lost responses retain the record.
Uploads stop on connection failures, authentication failures, rate limiting or
server errors. Permanently rejected records (400/409/422) stay in the buffer
marked as rejected and are skipped on subsequent cycles, until oldest-first
eviction. Uploads have both a request-count limit and an elapsed-time budget.

RTC memory tracks clock continuity through deep sleep. Readings collected before
the first successful time sync are converted using their captured RTC seconds,
not their position in the queue. A small `.clock-session` marker ties RTC state
to the filesystem: erasing/reflashing the filesystem invalidates retained RTC
metadata, so the first measurement is synchronized again instead of trusting an
old clock. The conversion is also saved to disk so that an
interrupted upload can resume after power loss. If power is lost **before any
successful time sync**, absolute times for that earlier RTC session cannot be
reconstructed: those records are retained until buffer eviction and are not
uploaded with invented dates. Previously timestamped readings remain sendable.
Legacy date-named `.txt` backups with valid timestamps are also supported.

Single-network MicroPython connects immediately and defaults to an eight-second
wait. `WIFI_TIMEOUT_S` changes the wait per profile (1–30 seconds); multi-network
discovery and attempts share a 45-second overall limit. HTTP uses the
configured socket timeout; DNS and a stuck native operation are also covered by
the hardware watchdog, which resets the board if execution stops progressing.
After a watchdog reset the next boot sleeps for one interval before retrying,
so a permanently stuck connection does not cause a rapid reset loop.
Sensor conversion has a one-second limit. Handled failures, including sensor,
configuration and storage errors, reach deep sleep. Awake time is subtracted
from the measurement interval instead of adding an arbitrary eight seconds.

## Observatory camera-server integration

The station can also send readings to the `remote_surv_camera.nosync` server.
Create a sensor in that server's Django Admin, register this station's MAC,
assign readers and copy its per-device token. In `firmware/.settings`, set
`API_url = 'https://YOUR-SERVER/api/measurements'` (no trailing slash) and
the corresponding `AUTH_TOKENS` entry (or legacy `AUTH_TOKEN`) to that sensor's
token. The endpoint accepts the existing
BME280 payload and provides `/timesync` and `/timedelta/<seconds>` in UTC.
See `remote_surv_camera.nosync/docs/climate.md` for setup and API details.

The updated `APIrequests.py` raises on unsuccessful HTTP responses and closes
responses even on errors. This keeps backup files until the server accepts
them, including when authentication fails or the server rate-limits requests.
Copy this updated file to the board as part of deployment.

Run the HTTP-error and backup-retention checks without hardware or networking:

```sh
python3 -m unittest discover -s local_dev -p 'test_*.py'
```

## Send 10 readings to the real API

`local_dev/run.py` simulates HTTP and does **not** send anything to the server.
Use the separate smoke test for real HTTP requests, without an ESP8266:

```sh
python3 local_dev/send_test_measurements.py --mac aa:bb:cc:dd:ee:ff
```

Replace the MAC with the exact address registered in **Admin > Sensors**.
The script reads `API_url` and selects the token for `--mac` from `AUTH_TOKENS`
(or reads the legacy `AUTH_TOKEN`) in the private
`firmware/.settings` file; Wi-Fi credentials are not used or displayed.
Use `--settings /path/to/.settings` for another configuration.

By default, it sends **10 synthetic temperatures from 20.0 to 24.5 degrees C**,
spaced five minutes apart over the last 45 minutes. It synchronizes with the
server clock, validates each saved ID and timestamp, and stops on the first
failure with a nonzero exit code. HTTP 201 means a new reading; HTTP 200 means
an identical reading was already saved. Check **Climate > 24 h** for the trend.
These test readings are stored in the selected sensor's real history.
For separate test data, provision a dedicated sensor with its own MAC and token.

Preview all payloads without making network requests:

```sh
python3 local_dev/send_test_measurements.py --mac aa:bb:cc:dd:ee:ff --dry-run
```

Optional controls: `--count 10`, `--temperature 20`, `--temperature-step 0.5`,
`--step-seconds 300`, `--delay 0.25`, `--timeout 10`.
The script uses only the Python standard library, does not print tokens,
does not execute the configuration file, and refuses HTTP redirects.

## Publishing source

Commit `.settings.example` with placeholder values. Keep your real `.settings`,
`.env` files, backups and local runtime state out of Git. The ignore rules cover
these files, but do not remove files that were already tracked; review
`git status` and the staged diff before publishing. If a credential was ever
committed, ignoring its file later does not remove it from history: rotate it
before sharing that history.
