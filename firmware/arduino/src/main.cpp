#include "station.h"
#include <Adafruit_BME280.h>
#include <Wire.h>
#include <Ticker.h>
#include <math.h>
#include <sys/time.h>
extern "C" {
#include <user_interface.h>
}

namespace {
Config config;
StationClock stationClock;
bool mounted = false, configured = false, clockReady = false, service = false;
uint32_t cycleStarted = 0;
Ticker cycleGuard;
String command;
constexpr uint32_t guardMagic = 0x47554152;

void armGuard() {
    cycleGuard.once(config.maxAwake, []() {
        uint32_t marker = guardMagic;
        ESP.rtcUserMemoryWrite(120, &marker, sizeof(marker));
        ESP.restart();
    });
}

void sleepFor(uint32_t milliseconds) {
    cycleGuard.detach();
    if (clockReady) stationClock.beforeSleep();
    Serial.printf("Deep sleep: %u ms\n", milliseconds);
    Serial.flush();
    if (mounted) LittleFS.end();
    ESP.deepSleep(static_cast<uint64_t>(milliseconds) * 1000, WAKE_RF_DEFAULT);
    delay(100);
    ESP.restart();
}

bool connectWifi() {
    return connectStationWifi(config);
}

bool networkTime() {
    // RTC time allows TLS after a brief NTP outage, with per-wake re-sync when
    // possible. A new/power-lost session requires network time before HTTPS.
    if (clockReady && stationClock.synced()) {
        timeval tv{static_cast<time_t>(stationClock.now()), 0};
        settimeofday(&tv, nullptr);
    }
    configTime(0, 0, config.ntpServer.c_str(), "pool.ntp.org", "time.google.com");
    const uint32_t start = millis();
    while (time(nullptr) < policy::epoch(2020, 1, 1, 0, 0, 0) && millis() - start < config.ntpTimeout * 1000) delay(50);
    if (time(nullptr) < policy::epoch(2020, 1, 1, 0, 0, 0)) {
        Serial.println(F("NTP unavailable; HTTPS deferred and measurements retained"));
        return false;
    }
    if (clockReady) stationClock.synchronize(time(nullptr));
    Serial.println(F("UTC time ready for certificate validation"));
    return true;
}

bool measuring() {
    Wire.beginTransmission(0x76);
    Wire.write(0xF3);
    if (Wire.endTransmission() != 0 || Wire.requestFrom(0x76, 1) != 1) return true;
    return (Wire.read() & 0x08) != 0;
}

bool measure(JsonDocument& record) {
    Wire.begin(4, 5); // SDA, SCL, matching the custom PCB.
    Wire.setClock(10000);
    Wire.setClockStretchLimit(1500);
    Adafruit_BME280 sensor;
    if (!sensor.begin(0x76, &Wire)) { Serial.println(F("BME280 not found at 0x76")); return false; }
    float temperature = 0, humidity = 0, pressure = 0;
    for (unsigned index = 0; index < 4; ++index) {
        sensor.setSampling(Adafruit_BME280::MODE_FORCED, Adafruit_BME280::SAMPLING_X8,
            Adafruit_BME280::SAMPLING_X8, Adafruit_BME280::SAMPLING_X8, Adafruit_BME280::FILTER_OFF);
        delay(80);
        uint32_t start = millis();
        while (measuring()) {
            if (millis() - start >= 1000) { Serial.println(F("BME280 conversion timeout")); return false; }
            delay(5);
        }
        const float t = sensor.readTemperature(), h = sensor.readHumidity(), p = sensor.readPressure() / 100;
        if (!isfinite(t) || !isfinite(h) || !isfinite(p) || t < -40 || t > 85 || h < 0 || h > 100 || p < 300 || p > 1100) {
            Serial.println(F("Invalid BME280 reading"));
            return false;
        }
        if (index) { temperature += t; humidity += h; pressure += p; }
        delay(100);
    }
    record["version"] = 1;
    JsonObject data = record.createNestedObject("data");
    data["temperature"] = round(temperature / 3 * 100) / 100;
    data["humidity"] = round(humidity / 3 * 100) / 100;
    data["pressure"] = round(pressure / 3 * 100) / 100;
    String mac = WiFi.macAddress();
    mac.toLowerCase();
    data["mac_address"] = mac;
    data["adc"] = round(analogRead(A0) * 4.2 / 1024 * 100) / 100;
    stationClock.capture(record);
    return !record.overflowed();
}

void probeHttps() {
    if (!configured) return;
    armGuard();
    if (!connectWifi() || !networkTime()) { cycleGuard.detach(); return; }
    Api api(config);
    int64_t epoch = -1;
    const int status = api.getTime(epoch);
    if (status == 200) {
        if (clockReady) stationClock.synchronize(epoch);
        Serial.println(F("HTTPS probe PASS: TLS 1.2, hostname and CA certificate validated; API time received"));
    } else Serial.printf("HTTPS probe FAILED: %d\n", status);
    cycleGuard.detach();
}

void runCycle() {
    if (!configured || !clockReady) { Serial.println(F("Cycle unavailable: configuration or RTC/storage initialization failed")); return; }
    cycleStarted = millis();
    armGuard();
    pinMode(13, INPUT_PULLUP);
    if (digitalRead(13) == LOW) {
        Serial.println(F("Charging input GPIO13 low: measurement cycle skipped"));
        cycleGuard.detach();
        return;
    }
    Queue queue(config);
    const bool storageReady = queue.begin();
    if (storageReady) queue.persistClock(stationClock);
    if (!storageReady) Serial.println(F("Queue storage unavailable; attempting current measurement directly"));
    DynamicJsonDocument record(1024);
    const bool valid = measure(record);
    bool queued = false;
    if (valid && storageReady) queued = queue.add(record);
    if (queued) Serial.println(F("Measurement queued"));
    else if (valid) Serial.println(F("Measurement could not be queued"));
    if (connectWifi() && networkTime()) {
        if (storageReady) queue.persistClock(stationClock);
        Api api(config);
        int64_t epoch = -1;
        if (api.getTime(epoch) == 200) {
            stationClock.synchronize(epoch);
            Serial.println(F("Time sync OK"));
        } else Serial.println(F("API time sync failed; using network/RTC UTC"));
        if (valid && !queued) {
            // This is the current boot's sample; never assign a new timestamp
            // to old samples from a session whose clock continuity was lost.
            const int64_t seconds = record["captured"].as<int64_t>() + stationClock.offset();
            record["data"]["timestamp"] = utcTimestamp(seconds);
            if (policy::accepted(api.post(record["data"].as<JsonVariantConst>()))) Serial.println(F("Measurement sent directly (storage unavailable)"));
        }
        record.clear();
        record.shrinkToFit();
        if (storageReady) {
            const uint32_t budget = config.maxAwake * 1000;
            const uint32_t elapsed = millis() - cycleStarted;
            const uint32_t remaining = elapsed < budget ? budget - elapsed : 0;
            const uint32_t requestBudget = (config.handshakeTimeout + 3 * config.timeout + 5) * 1000;
            if (remaining > requestBudget) queue.send(api, stationClock, millis() + std::min<uint32_t>(remaining - requestBudget, 20000));
            else Serial.println(F("Cycle time budget reached; queued measurements retained"));
        }
    }
    cycleGuard.detach();
}

void help() {
    Serial.println(F("Service console: help | status | list | https | run | selftest | sleep <seconds> | reboot"));
    Serial.println(F("Ctrl+C enters service mode; run performs a real measurement and upload"));
}

void status() {
    Serial.printf("Firmware: Arduino ESP8266; MAC: %s; free heap: %u; largest block: %u\n",
        WiFi.macAddress().c_str(), ESP.getFreeHeap(), ESP.getMaxFreeBlockSize());
    Serial.printf("Configuration: %s; filesystem: %s; RTC clock: %s; interval: %u s\n",
        configured ? "OK" : "unavailable", mounted ? "mounted" : "unavailable",
        clockReady && stationClock.synced() ? "synchronized" : "unsynchronized", config.interval);
    if (configured) Serial.println(config.token.length() ? F("API token: configured") : F("API token: missing for this MAC; uploads disabled"));
    if (configured && mounted) Queue(config).status();
    if (clockReady && stationClock.synced()) Serial.printf("RTC UTC: %s\n", utcTimestamp(stationClock.now()).c_str());
}

void execute(const String& line) {
    if (line == "help") help();
    else if (line == "status") status();
    else if (line == "https") probeHttps();
    else if (line == "run") { runCycle(); status(); }
    else if (line == "selftest" && configured && mounted) queueSelfTest(config);
    else if (line == "list" && configured && mounted) {
        for (const auto& file : Queue(config).files()) { Serial.println(file); yield(); }
    } else if (line == "reboot") ESP.restart();
    else if (line.startsWith("sleep ")) {
        String seconds = line.substring(6);
        bool digits = seconds.length() > 0 && seconds.length() <= 4;
        for (char c : seconds) digits = digits && isDigit(c);
        if (digits && seconds.toInt() >= 5 && seconds.toInt() <= 3600) sleepFor(seconds.toInt() * 1000);
        else Serial.println(F("Sleep must be 5-3600 seconds"));
    } else Serial.println(F("Unknown/unavailable command; type help"));
    Serial.print(F("station> "));
}

bool interruptRequested() {
    while (Serial.available()) if (Serial.read() == 3) return true;
    return false;
}
}

void setup() {
    Serial.begin(115200);
    Serial.println();
    Serial.println(F("Weather station: Arduino/BearSSL"));
    WiFi.persistent(false);
    const uint32_t cause = ESP.getResetInfoPtr()->reason;
    uint32_t guardMarker = 0, cleared = 0;
    ESP.rtcUserMemoryRead(120, &guardMarker, sizeof(guardMarker));
    ESP.rtcUserMemoryWrite(120, &cleared, sizeof(cleared));
    Serial.printf("Reset cause: %u\n", cause);
    if (cause != REASON_DEEP_SLEEP_AWAKE && cause != REASON_WDT_RST && cause != REASON_SOFT_WDT_RST) {
        Serial.println(F("Service window: press Ctrl+C within 3 s"));
        uint32_t start = millis();
        while (millis() - start < 3000 && !service) { service = interruptRequested(); delay(5); }
    }
    LittleFSConfig filesystem;
    filesystem.setAutoFormat(false);
    LittleFS.setConfig(filesystem);
    mounted = LittleFS.begin();
    if (mounted) {
        configured = config.load();
        clockReady = stationClock.begin(cause == REASON_DEEP_SLEEP_AWAKE);
    } else Serial.println(F("Filesystem mount failed; automatic formatting is disabled"));
    if (cause == REASON_WDT_RST || cause == REASON_SOFT_WDT_RST ||
        (cause == REASON_SOFT_RESTART && guardMarker == guardMagic)) {
        if (!service) {
            Serial.println(F("Reset recovery: sleeping before retry"));
            sleepFor(config.interval * 1000);
        }
    }
#ifdef STATION_TLS_PROBE
    service = true;
    probeHttps();
#endif
    if ((!configured || !clockReady) && !service) sleepFor(config.interval * 1000);
    if (service) { help(); status(); Serial.print(F("station> ")); return; }
    runCycle();
    if (interruptRequested()) { service = true; help(); Serial.print(F("station> ")); return; }
    sleepFor(policy::sleepRemainder(config.interval * 1000, millis() - cycleStarted));
}

void loop() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == 3) { command = String(); help(); Serial.print(F("station> ")); }
        else if (c == '\r' || c == '\n') {
            if (command.length()) { command.trim(); execute(command); command = String(); }
        } else if (c == 8 || c == 127) { if (command.length()) command.remove(command.length() - 1); }
        else if (c >= 32 && command.length() < 80) command += c;
    }
    delay(10); // Feed the SDK watchdog and keep the Wi-Fi stack progressing.
}
