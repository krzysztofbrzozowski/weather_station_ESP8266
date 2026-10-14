# ESP8266 weather station — Arduino

Native C++ implementation for the existing 4 MiB ESP8266 PCB. Build it with
PlatformIO; Arduino IDE is not required. The original MicroPython project stays
in `firmware/micropython/src`.

## Behavior

The station takes four BME280 forced readings, discards the first, averages the
remaining three, queues the result, connects to Wi-Fi, obtains UTC, uploads the
queue and enters deep sleep. Awake time is subtracted from the configured
interval. GPIO13 LOW skips the measurement cycle, preserving the existing
charging-input behavior.

HTTPS uses BearSSL with TLS 1.2, hostname verification, CA trust anchors and
certificate validity checks. The bundled public certificates are Let's Encrypt
ISRG Root X1 and X2. A different API may require adding its root CA to `data/ca.pem`
before rebuilding the filesystem. Certificates come from
[Let's Encrypt](https://letsencrypt.org/certificates/). Certificate verification
is required; there is no automatic HTTP or insecure TLS fallback.

`certs/isrg-roots.pem` contains public root CA certificates, without private
keys or device credentials. Keep it in Git so a fresh checkout can build the
same trust store. Private keys (`*.key`), real `.settings`, staged `data/`, build
images and device backups remain ignored.

UTC is obtained using NTP before the first TLS connection. RTC state tracks
relative elapsed time across timed sleep/wake cycles; an NTP/API synchronization
maps the current session to UTC. After power loss or manual reset, old samples
without a trustworthy time mapping are retained without inventing timestamps.
Already timestamped readings keep their original dates.

The LittleFS queue retains the MicroPython JSON record format, including clock
maps and rejected records. It commits files through `.tmp` + atomic rename,
recovers complete interrupted writes and evicts the oldest owned readings when
the record limit or free-space reserve is reached. A record is removed only
after HTTP 200/201. HTTP 400/409/422 are retained and skipped; authentication,
rate-limit, TLS and server errors stop the batch and retain the queue.

The SDK watchdog remains enabled. A separate 90-second cycle guard resets an
overdue cycle, and the next boot sleeps for an interval before retrying. Wi-Fi,
NTP, HTTP and queue batches also have their own limits. Filesystem mount failure
never triggers automatic formatting.

## Layout and pins

| Resource | Setting |
|---|---|
| ESP8266 | 4 MiB flash, 160 MHz |
| Application | Up to 1 MiB; compiled binary starts at `0x000000` |
| LittleFS | `0x300000`–`0x3FA000`, 8 KiB blocks |
| BME280 | Address `0x76`, SDA GPIO4, SCL GPIO5, 10 kHz |
| Charging input | GPIO13, input pull-up, LOW skips cycle |
| Battery | A0, existing `raw * 4.2 / 1024` scaling |
| Timed wake | GPIO16 → RST, SJ2 closed |
| Serial console | 115200 baud |

On this PCB, open **SJ2 during copying/programming** if GPIO16 interferes with
reset. Close it again before testing automatic wake. The Arduino low-power
example recommends a Schottky diode between RST (anode) and GPIO16 (cathode) when
a reset switch must coexist with the wake connection:
[official example](https://github.com/esp8266/Arduino/blob/master/libraries/esp8266/examples/LowPowerDemo/LowPowerDemo.ino).

## Install and build

Run these commands from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install platformio==6.2.0 esptool==5.5.0 mpremote==1.29.0 littlefs-python==0.19.0

pio run -d firmware/arduino -e tls_probe -e station
```

`platformio.ini` pins the ESP8266 platform, Arduino core and application
libraries. Downloads are stored in the ignored `.pio-core` directory, and
compiled artifacts are under `.pio/build`.

`tls_probe` boots into the service console and checks the real HTTPS time
endpoint, including certificate validation. It does not take measurements or
POST them automatically. `station` runs the full measurement/sleep cycle.
The flash helper defaults to `station` and prints the selected image and device
MAC before writing. `tls_probe` must be selected explicitly for diagnostics.

## Build and flash with one script

Use the repository's `platformio.sh`; it selects the local `.venv` and
PlatformIO core directory automatically. From the repository root:

```sh
./platformio.sh --build
./platformio.sh --flash --port /dev/cu.usbserial-210 --cycles 3
```

`--build` validates `firmware/.settings`, regenerates the private filesystem,
builds **station** and `littlefs.bin`, and checks that the filesystem contains
exactly the current settings. It reports the interval, profile/token counts and
image SHA256 hashes, without printing credentials. A manifest is saved in the
ignored `.pio/build/station/deploy-manifest.json`.

`--flash` runs that same build first, verifies the manifest and writes both
images. Any failed preparation/build/check stops the script; stale images are
never used as a fallback. It then verifies a real measurement upload and sleep.
`--cycles 3` additionally checks two timed wakes with reset cause 5 and another
successful upload after each wake, keeping the serial port continuously open.
The default is one cycle. UART evidence is saved in
`.pio/build/station/first-boot.log`; failed runtime verification returns an error.

**Default flashing replaces the filesystem, including pending measurements.**
For an update retaining device settings and the queue:

```sh
./platformio.sh --flash --code-only --port /dev/cu.usbserial-210
```

Close other serial terminals before flashing. On this user's setup, use BOOT
when the tool prints `Connecting`; release BOOT once the flasher stub starts.
SJ2 can stay closed if programming works with it closed, and must be closed for
timed wake. Open it only if it interferes with reset on your hardware. If manual
bootloader entry is needed, use `--manual-reset` and the board's BOOT/RST controls.
`ESP_PORT` can supply the port instead of `--port`. No shell activation or other
exports are required. The script can also be called by absolute path from
another working directory.

## Private configuration

Continue editing the existing `firmware/.settings`. Convert its literal
Python assignments into the native `/config.json` with:

```sh
python firmware/arduino/tools/prepare.py stage
pio run -d firmware/arduino -e tls_probe -t buildfs
```

`stage` generates the ignored `firmware/arduino/data` directory. It accepts
strings, integers, MAC/token dictionaries, Wi-Fi profile lists and comments,
without executing Python. Use `--replace` to
explicitly regenerate an existing host staging directory. The API must use
HTTPS, with no embedded credentials, query string or fragment. Wi-Fi credentials
and the API token are stored in the private filesystem image, not in C++ source.

API URL, Wi-Fi, backup directory and queue controls retain their names. Tokens
can be configured in a shared map:

```python
AUTH_TOKENS = {
    'dc:4f:22:7c:3a:fa': 'Your-token-for-first-device',
    'dc:4f:22:7c:3a:fc': 'Your-token-for-second-device',
}
```

The generated filesystem contains the complete map; each board selects its own
token from its Wi-Fi station MAC at startup. The same firmware and filesystem
image can therefore be flashed onto all boards. MACs accept uppercase/lowercase
and colon/hyphen separators. Maps must have 1–100 entries, with no duplicate MACs
after normalization; tokens must contain 1–256 printable ASCII characters without
spaces. The complete configuration is limited to 32768 bytes.
The Arduino loader streams the JSON from flash and retains only general
settings and this board's token in a 4 KiB JSON document. Other devices' tokens
are skipped during parsing; increasing the registry to 100 entries does not
allocate the full registry in RAM. The preparation tool validates and
canonicalizes all entries before generating the image.

Unknown MACs print `No API token for MAC ...; uploads disabled`, keep collecting
and queueing measurements and follow the normal sleep interval. No POST is sent
until a matching token is configured. `status` reports whether a token is
configured, without exposing it. Other devices' tokens are not used as fallback.

The legacy `AUTH_TOKEN = 'Your API token'` format remains supported when the map
is absent. When `AUTH_TOKENS` exists, it takes precedence over `AUTH_TOKEN`.
After adding/changing a map, use the code-and-settings upload procedure below;
older firmware only understands the legacy single token.

### Multiple Wi-Fi networks

Configure 1–4 profiles in the shared `.settings`:

```python
WIFI_NETWORKS = [
    {'SSID': 'Home-WiFi', 'PASSWD': 'Your-home-password'},
    {'SSID': 'Office-WiFi', 'PASSWD': 'Your-office-password'},
    {'SSID': 'Workshop-WiFi', 'PASSWD': 'Your-workshop-password'},
    {'SSID': 'Phone-Hotspot', 'PASSWD': 'Your-hotspot-password'},
]
```

On first boot the station scans for configured visible SSIDs and attempts them
in list order. The successful profile is saved in `/.wifi.json` and is tried
first on later wakes and after power loss. If it disappears or authentication
fails, the station scans again and attempts other visible profiles. A failed
scan falls back to direct attempts. Failed connections never replace the saved
preference; it is written only when the successful profile changes.

Each SSID accepts 1–32 UTF-8 bytes and each password 0–64 bytes (empty means an
open network). Exact duplicate profiles are rejected; the same SSID with
different passwords is allowed. The cache contains an index and a fingerprint,
without plaintext passwords. Editing/reordering profiles invalidates the cache.

`WIFI_TIMEOUT_S` limits each connection attempt. Scanning is limited to 5
seconds; the whole discovery/connection phase is limited to the smaller of
`5 + WIFI_TIMEOUT_S * profile_count` seconds and half of `MAX_AWAKE_S`.
Remaining time is shared between pending visible profiles so early failures
cannot consume the entire budget before the final profile is tried.
When none succeeds, measurements remain queued and discovery retries next cycle.
The legacy `SSID`/`PASSWD` pair remains supported; a present `WIFI_NETWORKS` list
takes precedence. One profile can be attempted directly without a scan.

Additional Arduino settings, with defaults:

```python
WIFI_TIMEOUT_S = 12
NTP_TIMEOUT_S = 15
MAX_AWAKE_S = 90
NTP_SERVER = 'time.cloudflare.com'
```

The pinned core uses a 15-second TLS handshake timeout. `HTTP_TIMEOUT_S` controls
HTTP socket operations separately and accepts 1–10 seconds. The application
guard bounds the full cycle, including slow cryptographic work.

The queue record limit is an upper bound; the filesystem's space reserve may
cause earlier eviction. This 1 MiB native filesystem has less space than the
original MicroPython filesystem.

## Updating settings and firmware

Edit the shared `firmware/.settings`, used by the Arduino preparation tool and
the MicroPython local launcher. For example:

```python
API_url = 'https://your-server.example/api/measurements'
AUTH_TOKENS = {
    'dc:4f:22:7c:3a:fa': 'Your-token-for-first-device',
    'dc:4f:22:7c:3a:fc': 'Your-token-for-second-device',
}
WIFI_NETWORKS = [
    {'SSID': 'Home-WiFi', 'PASSWD': 'Your-home-password'},
    {'SSID': 'Office-WiFi', 'PASSWD': 'Your-office-password'},
]
BACKUP_FOLDER = 'backup_logs'
MEASUREMENT_INTERVAL_S = 30
```

`MEASUREMENT_INTERVAL_S` is the interval between cycle starts, in seconds:
`30` is 30 seconds, `300` is 5 minutes, and `1800` is 30 minutes. Awake time is
subtracted from sleep. If the cycle exceeds the interval, the station sleeps
for at least one second before starting again. This is not a fixed delay added
after sending. Editing this file does not change an already running device.

To rebuild and install both code and settings, run from the repository root:

```sh
./platformio.sh --flash --port /dev/cu.usbserial-210 --cycles 3
```

The script rebuilds settings and both images before opening the port. Use BOOT
as described above if automatic entry needs help. Keep SJ2 closed for timed wake.

**Uploading the filesystem replaces its contents, including pending
measurements.** Use this procedure for a fresh configuration when losing the
current queue is acceptable. Credentials are present in the generated private
image; these files are ignored by Git.

For code changes with existing device settings and queue, omit filesystem
preparation and upload:

```sh
./platformio.sh --flash --code-only --port /dev/cu.usbserial-210
```

`prepare.py stage` also accepts `--settings /path/to/.settings` if a separate
Arduino configuration file is preferred.

## Optional: preserve device data before migration

The Arduino filesystem has a different flash layout. To retain existing data,
obtain and verify a backup before replacing the filesystem. Keep backups private:
they contain Wi-Fi credentials and the API token.

For a working MicroPython serial connection:

```sh
export ESP_PORT=/dev/cu.usbserial-210
python firmware/arduino/tools/prepare.py export --port "$ESP_PORT"
```

The export waits for a reset or wake. Press RST while it is waiting. It writes
a timestamped `device_backups/<timestamp>/files` directory and a checksum
manifest. Incomplete exports are refused by the migration tool.

For unstable MicroPython communication, a full flash snapshot can be read with
esptool in ROM bootloader mode:

```sh
python -m esptool --chip esp8266 --port "$ESP_PORT" --baud 115200 \
  read-flash 0x0 0x400000 firmware/arduino/device_backups/micropython-full-flash.bin
python firmware/arduino/tools/prepare.py extract \
  --image firmware/arduino/device_backups/micropython-full-flash.bin
```

Enter the bootloader by holding GPIO0 LOW during reset; release RST first.
Adapter-controlled bootloader entry depends on your programming connector.
The extraction tool discovers the MicroPython LittleFS superblock, mounts it
without formatting, extracts files and creates a verified export. It does not
modify the snapshot. A complete flash snapshot also allows restoring the
original interpreter and filesystem together; RTC RAM is not part of flash.

Pass the complete export directory printed by either method:

```sh
python firmware/arduino/tools/prepare.py stage --replace \
  --export firmware/arduino/device_backups/EXPORT_DIRECTORY
pio run -d firmware/arduino -e tls_probe -t buildfs
```

Migration copies the queue directory, clock mapping, pending temporary files and
rejection markers. File contents and original timestamps are preserved. It uses
the exported device's `BACKUP_FOLDER` as the source and the current local setting
as the destination.

## Optional: TLS diagnostics and manual flashing

With the backup verified and the migrated filesystem prepared, enter the ROM
bootloader and upload both images. These offsets apply to the pinned 4 MiB
configuration only:

```sh
python -m esptool --chip esp8266 --port "$ESP_PORT" --baud 115200 \
  write-flash --flash-mode dio --flash-freq 40m --flash-size 4MB \
  0x000000 firmware/arduino/.pio/build/tls_probe/firmware.bin \
  0x300000 firmware/arduino/.pio/build/tls_probe/littlefs.bin
python -m serial.tools.miniterm "$ESP_PORT" 115200 --raw
```

Restart in normal boot mode, with GPIO0 released HIGH. A successful probe prints
`HTTPS probe PASS: TLS 1.2, hostname and CA certificate validated; API time received`.
The `run` console command then performs a real measurement and POST using the
configured token. HTTP 401 requires checking that this MAC is registered and
that the token belongs to that sensor.

After the probe passes, install the full station application. Preserve the
filesystem during subsequent code updates:

```sh
pio run -d firmware/arduino -e station
python -m esptool --chip esp8266 --port "$ESP_PORT" --baud 115200 \
  write-flash --flash-mode dio --flash-freq 40m --flash-size 4MB \
  0x000000 firmware/arduino/.pio/build/station/firmware.bin
```

Re-uploading the filesystem replaces its current contents with the host image.
For ordinary code updates, upload only `firmware.bin`.

## Service console

Connect at 115200 baud, press RST and send **Ctrl+C** within the printed
three-second service window. Timed deep-sleep wake skips the window. A sleeping
ESP8266 needs RST before it can receive serial input. This firmware provides a
command console rather than the Python REPL.

| Command | Action |
|---|---|
| `help` | List commands |
| `status` | Show MAC, memory, interval and queue count; never print credentials |
| `list` | List queue filenames |
| `https` | Validate HTTPS and fetch API time, without POST |
| `run` | Take a real measurement, queue it and attempt upload |
| `selftest` | Check circular eviction and interrupted-write recovery in a separate temporary directory; no test POSTs |
| `sleep 10` | Sleep for 10 seconds, then boot into the installed application |
| `reboot` | Restart the application |

Use Ctrl+] to leave Python's miniterm. Keep one serial connection open when
checking timed wake; adapter reset-line changes can otherwise imitate a wake.
Confirm elapsed sleep, reset cause 5, successful upload and the next sleep.

## Hardware checks

With SJ2 open, this helper resets into service mode, verifies HTTPS, exercises
the isolated queue and uploads a real BME280 measurement:

```sh
python firmware/arduino/tools/check_device.py --port "$ESP_PORT" \
  --boot --service --reset --commands https selftest run status
```

On the target PCB, the 2026-10-14 check confirmed validated TLS 1.2, API time
GET HTTP 200, BME280 measurement POST HTTP 201, an empty queue after upload,
oldest-record eviction and recovery of complete/incomplete temporary files.
Around 45 KiB heap remained after the upload. The installed private settings
currently select a 1800-second (30-minute) measurement interval.

With SJ2 closed and serial continuously open, `sleep 10` produced an automatic
wake after 10.19 seconds with reset cause 5. The following cycle completed API
GET HTTP 200 and measurement POST HTTP 201, then entered sleep for 1,782,681 ms
(the configured 30-minute interval minus that cycle's awake time).

For an automated sleep check, keep the same connection open while closing SJ2:

```sh
SLEEP_CHECK_DIR=$(mktemp -d)
echo "$SLEEP_CHECK_DIR/ready"
python firmware/arduino/tools/check_device.py --port "$ESP_PORT" \
  --boot --service --reset --commands https selftest run status \
  --sleep 10 --sleep-ready-file "$SLEEP_CHECK_DIR/ready"
```

After the helper prints `Checks complete; keeping serial open`, close SJ2
without resetting. From a second terminal, create the printed ready-file path
with `touch /printed/path/ready`. The helper verifies a timed wake with reset
cause 5, another real POST and entry into the next sleep. UART evidence is saved
under the ignored `.pio/hardware-check.log`. This test does not measure current
draw or establish long-term stability.

## Host checks

```sh
python -m unittest discover -s firmware/arduino/tests -p 'test_*.py'
clang++ -std=c++11 -Wall -Wextra -Werror -Ifirmware/arduino/include \
  firmware/arduino/tests/policy_test.cpp -o /tmp/weather-station-policy-test
/tmp/weather-station-policy-test
clang++ -std=c++11 -Wall -Wextra -Werror -Ifirmware/arduino/include \
  -Ifirmware/arduino/.pio/libdeps/station/ArduinoJson/src \
  firmware/arduino/tests/auth_test.cpp -o /tmp/weather-station-auth-test
/tmp/weather-station-auth-test
clang++ -std=c++11 -Wall -Wextra -Werror -Ifirmware/arduino/include \
  -Ifirmware/arduino/.pio/libdeps/station/ArduinoJson/src \
  firmware/arduino/tests/wifi_test.cpp -o /tmp/weather-station-wifi-test
/tmp/weather-station-wifi-test
```

Host checks cover date validation, HTTP acknowledgement/rejection policy,
interval arithmetic, RTC checksum changes, private configuration validation,
export checksums and lossless migration/extraction.
MAC/token selection is also tested against the same C++ implementation used on
the board, including missing MACs, map precedence and invalid/duplicate entries.
The production streaming reader is also exercised with 100 distinct
256-character tokens, selecting the first, middle and last entry and an unknown
MAC within the same fixed JSON memory budget.
Wi-Fi tests cover profile validation, preferred-network reuse, discovery,
password failures, fallback, preference invalidation and bounded offline waits.
The C++ ordering policy used by the board is tested with simulated connections;
physical roaming between access points still requires hardware verification.
These checks do not establish real TLS memory use, I2C operation, power
consumption or long-term hardware stability;
those require testing on the board.
