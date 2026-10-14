#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTION=""
PORT="${ESP_PORT:-/dev/cu.usbserial-210}"
CYCLES=1
CODE_ONLY=0
MANUAL=0
LED_TEST=0
CYCLES_SET=0

usage() {
    cat <<'EOF'
Usage:
  ./platformio.sh --build
  ./platformio.sh --flash [--port PORT] [--cycles 0..5 | --led-test] [--code-only] [--manual-reset]

--build         Prepare firmware/.settings, build station and its filesystem, verify images.
--flash         Run the same build, flash both images, verify measurement/sleep cycles.
--cycles N      Number of cycles to check (default: 1); 0 exits after flashing/reset.
--code-only     Preserve settings and measurement queue already on the device.
--manual-reset  Wait for manual bootloader entry instead of automatic reset.
--led-test      Flash, enter service mode and identify the LED pin; no measurement cycles.

Default --flash replaces the device filesystem, including pending measurements.
Uses this checkout's .venv automatically. No activation or exports are required.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --build|--flash)
            [[ -z "$ACTION" ]] || { usage >&2; exit 2; }
            ACTION="$1"; shift ;;
        --port|--cycles)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            if [[ "$1" == --port ]]; then PORT="$2"; else CYCLES="$2"; CYCLES_SET=1; fi
            shift 2 ;;
        --code-only) CODE_ONLY=1; shift ;;
        --manual-reset) MANUAL=1; shift ;;
        --led-test) LED_TEST=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ -n "$ACTION" && "$CYCLES" =~ ^[0-5]$ ]] || { usage >&2; exit 2; }
if [[ "$ACTION" == --build && ( "$CODE_ONLY" == 1 || "$MANUAL" == 1 || "$LED_TEST" == 1 ) ]]; then
    usage >&2; exit 2
fi
if [[ "$LED_TEST" == 1 && "$CYCLES_SET" == 1 ]]; then
    usage >&2; exit 2
fi

cd "$PROJECT_ROOT"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
TOOLS="$PROJECT_ROOT/firmware/arduino/tools"
[[ -x "$PYTHON" ]] || { echo 'Missing .venv; install dependencies from firmware/arduino/README.md.' >&2; exit 1; }
export PLATFORMIO_CORE_DIR="$PROJECT_ROOT/firmware/arduino/.pio-core"
export PLATFORMIO_SETTING_ENABLE_TELEMETRY=no
trap 'printf "Stopped after an error at script line %s.\n" "$LINENO" >&2' ERR

# Every flash follows this exact build path; failures never reuse old images.
rm -f "$PROJECT_ROOT/firmware/arduino/.pio/build/station/deploy-manifest.json"
printf 'Project: %s\nFirmware: station\n' "$PROJECT_ROOT"
"$PYTHON" "$TOOLS/prepare.py" stage --replace
"$PYTHON" -m platformio run -d "$PROJECT_ROOT/firmware/arduino" -e station
"$PYTHON" -m platformio run -d "$PROJECT_ROOT/firmware/arduino" -e station -t buildfs
"$PYTHON" "$TOOLS/deploy_manifest.py" record

if [[ "$ACTION" == --flash ]]; then
    "$PYTHON" "$TOOLS/deploy_manifest.py" verify
    FLASH_ARGS=(write --port "$PORT" --environment station --connect-attempts 0)
    if [[ "$LED_TEST" == 1 ]]; then
        FLASH_ARGS+=(--led-test)
    elif [[ "$CYCLES" == 0 ]]; then
        FLASH_ARGS+=(--no-monitor)
    else
        FLASH_ARGS+=(--verify-cycles "$CYCLES")
    fi
    if [[ "$CODE_ONLY" == 0 ]]; then
        echo 'Uploading firmware and settings; the existing measurement queue will be replaced.'
        FLASH_ARGS+=(--with-filesystem)
    else
        echo 'Uploading firmware; existing device settings and measurement queue are retained.'
    fi
    if [[ "$MANUAL" == 1 ]]; then FLASH_ARGS+=(--manual-reset); fi
    "$PYTHON" "$TOOLS/flash.py" "${FLASH_ARGS[@]}"
else
    echo 'Build verified. ./platformio.sh --flash rebuilds and uploads the current settings.'
fi
