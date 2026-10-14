#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#include <vector>
#include "record_policy.h"

struct Config {
    struct Network { String ssid, password; } networks[4];
    unsigned networkCount = 0;
    String apiUrl, token, folder;
    uint32_t interval = 300, timeout = 3, maxRecords = 144, reserveBytes = 32768, maxSend = 6;
    uint32_t wifiTimeout = 12, ntpTimeout = 15, maxAwake = 90;
    static constexpr uint32_t handshakeTimeout = 15; // Fixed by Arduino core 3.1.2.
    String ntpServer = "time.cloudflare.com", caFile = "/ca.pem";
    bool load();
};

bool connectStationWifi(const Config& config);

bool readJson(const String& path, JsonDocument& doc, size_t maxBytes = 2048);
bool writeJson(const String& path, const JsonDocument& doc);
String utcTimestamp(int64_t seconds);
bool queueSelfTest(const Config& config);

class StationClock {
    struct State {
        uint32_t magic, version, crc, lastTicks, calibration, synced;
        uint64_t elapsedMs;
        int64_t offset;
        char session[20];
    } state_{};
    void checkpoint();
    void save();
public:
    bool begin(bool timedWake);
    uint64_t seconds();
    String session() const { return String(state_.session); }
    bool synced() const { return state_.synced; }
    int64_t offset() const { return state_.offset; }
    int64_t now();
    void synchronize(int64_t epoch);
    void capture(JsonDocument& record);
    void beforeSleep();
};

class Api {
    const Config& config_;
public:
    explicit Api(const Config& config) : config_(config) {}
    int getTime(int64_t& epoch);
    int post(JsonVariantConst data);
private:
    int request(bool post, JsonVariantConst data, JsonDocument* reply);
};

class Queue {
    const Config& config_;
    String path(const String& name) const { return "/" + config_.folder + "/" + name; }
    bool read(const String& name, JsonDocument& doc);
    bool makeSpace(size_t bytes, const String& exclude, bool adding);
    bool write(const String& name, const JsonDocument& doc, bool adding = false);
    bool stamp(JsonDocument& record, JsonVariantConst map);
    bool rememberClock(StationClock& clock);
public:
    explicit Queue(const Config& config) : config_(config) {}
    bool begin();
    std::vector<String> files() const;
    bool add(const JsonDocument& record, String* storedName = nullptr);
    bool persistClock(StationClock& clock) { return rememberClock(clock); }
    unsigned send(Api& api, StationClock& clock, uint32_t deadline,
                  const String& currentRecord = String(), bool* currentAccepted = nullptr);
    void status() const;
};
