#include "station.h"
#include "auth_config.h"
#include "wifi_policy.h"
#include <algorithm>
#include <time.h>
extern "C" {
#include <user_interface.h>
}

bool readJson(const String& path, JsonDocument& doc, size_t maxBytes) {
    File file = LittleFS.open(path, "r");
    return file && file.size() <= maxBytes && !deserializeJson(doc, file, DeserializationOption::NestingLimit(6));
}

bool writeJson(const String& path, const JsonDocument& doc) {
    if (doc.overflowed()) return false;
    const String temporary = path + ".tmp";
    File file = LittleFS.open(temporary, "w");
    if (!file) return false;
    const size_t expected = measureJson(doc);
    const size_t written = serializeJson(doc, file);
    file.flush();
    file.close();
    if (written != expected) return false;
    // LittleFS rename replaces the destination atomically; keep it until commit.
    return LittleFS.rename(temporary, path);
}

String utcTimestamp(int64_t seconds) {
    if (seconds < policy::epoch(2020, 1, 1, 0, 0, 0) || seconds > policy::epoch(2099, 12, 31, 23, 59, 59)) return String();
    time_t raw = static_cast<time_t>(seconds);
    struct tm value{};
    gmtime_r(&raw, &value);
    char buffer[24];
    strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &value);
    return String(buffer);
}

bool Config::load() {
    DynamicJsonDocument doc(auth::configCapacity);
    const String mac = WiFi.macAddress();
    File configuration = LittleFS.open("/config.json", "r");
    const bool loaded = configuration && configuration.size() <= auth::maxConfigBytes &&
        auth::readConfig(doc, configuration, mac.c_str());
    configuration.close();
    if (!loaded) {
        Serial.println(F("Cannot load /config.json; prepare and upload the filesystem first"));
        return false;
    }
    for (const char* key : {"API_url", "BACKUP_FOLDER"}) {
        if (!doc[key].is<const char*>()) {
            Serial.printf("Missing/invalid setting: %s\n", key);
            return false;
        }
    }
    apiUrl = doc["API_url"].as<String>();
    while (apiUrl.endsWith("/")) apiUrl.remove(apiUrl.length() - 1);
    const auto selected = auth::selectFiltered(doc.as<JsonVariantConst>(), mac.c_str());
    if (!selected.valid) {
        Serial.println(F("Invalid AUTH_TOKENS map or AUTH_TOKEN"));
        return false;
    }
    token = selected.token ? selected.token : "";
    if (!selected.token) Serial.printf("No API token for MAC %s; uploads disabled; measurements will be queued\n", mac.c_str());
    wifi::Profile profiles[wifi::maxProfiles];
    networkCount = wifi::readProfiles(doc.as<JsonVariantConst>(), profiles);
    if (networkCount == 0) {
        Serial.println(F("Invalid WIFI_NETWORKS or SSID/PASSWD; configure 1-4 profiles"));
        return false;
    }
    for (unsigned i = 0; i < networkCount; ++i) {
        networks[i].ssid = profiles[i].ssid;
        networks[i].password = profiles[i].password;
    }
    folder = doc["BACKUP_FOLDER"].as<String>();
    if (!apiUrl.startsWith("https://") || apiUrl.indexOf('@') >= 0 || apiUrl.indexOf('?') >= 0 ||
        apiUrl.indexOf('#') >= 0 || apiUrl.indexOf('\r') >= 0 || apiUrl.indexOf('\n') >= 0 ||
        folder.length() == 0 || folder.length() > 24 || folder == "." || folder == ".." ||
        folder.indexOf('/') >= 0 || folder.indexOf('\\') >= 0) {
        Serial.println(F("Invalid API URL, token, Wi-Fi or backup directory"));
        return false;
    }
    struct Number { const char* key; uint32_t* target; uint32_t low, high; } numbers[] = {
        {"MEASUREMENT_INTERVAL_S", &interval, 10, 3600}, {"HTTP_TIMEOUT_S", &timeout, 1, 10},
        {"BACKUP_MAX_RECORDS", &maxRecords, 1, 512}, {"BACKUP_MIN_FREE_BYTES", &reserveBytes, 8192, 524288},
        {"BACKUP_MAX_SEND", &maxSend, 1, 20}, {"WIFI_TIMEOUT_S", &wifiTimeout, 1, 30},
        {"NTP_TIMEOUT_S", &ntpTimeout, 1, 30},
        {"MAX_AWAKE_S", &maxAwake, 30, 180}
    };
    for (auto& n : numbers) {
        if (doc.containsKey(n.key)) {
            if (!doc[n.key].is<uint32_t>() || doc[n.key].is<bool>() || doc[n.key] < n.low || doc[n.key] > n.high) {
                Serial.printf("Invalid numeric setting: %s\n", n.key);
                return false;
            }
            *n.target = doc[n.key].as<uint32_t>();
        }
    }
    ntpServer = doc["NTP_SERVER"] | "time.cloudflare.com";
    caFile = doc["CA_FILE"] | "/ca.pem";
    return ntpServer.length() > 0 && caFile.startsWith("/") && caFile.length() <= 31;
}

void StationClock::checkpoint() {
    const uint32_t ticks = system_get_rtc_time();
    const uint64_t micros = (static_cast<uint64_t>(ticks - state_.lastTicks) * state_.calibration) >> 12;
    state_.elapsedMs += micros / 1000;
    state_.lastTicks = ticks;
}

void StationClock::save() {
    state_.crc = 0;
    state_.crc = policy::checksum(&state_, sizeof(state_));
    ESP.rtcUserMemoryWrite(32, reinterpret_cast<uint32_t*>(&state_), sizeof(state_));
}

bool StationClock::begin(bool timedWake) {
    ESP.rtcUserMemoryRead(32, reinterpret_cast<uint32_t*>(&state_), sizeof(state_));
    const uint32_t storedCrc = state_.crc;
    state_.crc = 0;
    bool valid = timedWake && state_.magic == 0x57535441 && state_.version == 1 &&
        storedCrc == policy::checksum(&state_, sizeof(state_)) && state_.calibration != 0 &&
        state_.synced <= 1 && state_.session[16] == '\0';
    File marker = LittleFS.open("/.arduino-session", "r");
    if (valid) valid = marker && marker.size() == 16 && marker.readString() == String(state_.session);
    marker.close();
    if (valid) checkpoint();
    else {
        memset(&state_, 0, sizeof(state_));
        state_.magic = 0x57535441;
        state_.version = 1;
        state_.lastTicks = system_get_rtc_time();
        state_.calibration = system_rtc_clock_cali_proc();
        snprintf(state_.session, sizeof(state_.session), "%08lx%08lx", os_random(), os_random());
        File next = LittleFS.open("/.arduino-session.tmp", "w");
        bool committed = next && next.print(state_.session) == 16;
        if (next) { next.flush(); next.close(); }
        if (committed) committed = LittleFS.rename("/.arduino-session.tmp", "/.arduino-session");
        if (!committed) Serial.println(F("Clock marker unavailable; next boot will use a new clock session"));
    }
    save();
    return true;
}

uint64_t StationClock::seconds() { checkpoint(); return state_.elapsedMs / 1000; }
int64_t StationClock::now() { return state_.synced ? static_cast<int64_t>(seconds()) + state_.offset : -1; }
void StationClock::synchronize(int64_t epoch) {
    // Existing absolute timestamps are immutable; update the current session's
    // relative-to-UTC conversion when a better network clock is available.
    state_.offset = epoch - static_cast<int64_t>(seconds());
    state_.synced = 1;
    save();
}
void StationClock::capture(JsonDocument& record) {
    record["clock"] = session();
    record["captured"] = seconds();
    // Assign UTC only after this cycle's synchronization, immediately before
    // the first POST. A stale RTC estimate must not freeze a future timestamp.
}
void StationClock::beforeSleep() { checkpoint(); save(); }

static bool isRecord(const String& name) {
    if (name.startsWith("D") && name.endsWith(".txt")) return true;
    if (!name.startsWith("q") || !name.endsWith(".json") || name.length() < 7 || name.length() > 26) return false;
    for (unsigned i = 1; i < name.length() - 5; ++i) if (!isDigit(name[i])) return false;
    return true;
}

std::vector<String> Queue::files() const {
    std::vector<String> names;
    Dir dir = LittleFS.openDir("/" + config_.folder);
    while (dir.next()) {
        String name = dir.fileName();
        name = name.substring(name.lastIndexOf('/') + 1);
        if (isRecord(name)) names.push_back(name);
        yield();
    }
    std::sort(names.begin(), names.end(), [](const String& a, const String& b) {
        if (a.startsWith("q") && b.startsWith("q")) return strtoull(a.c_str() + 1, nullptr, 10) < strtoull(b.c_str() + 1, nullptr, 10);
        return a < b;
    });
    return names;
}

bool Queue::read(const String& name, JsonDocument& doc) {
    if (!readJson(path(name), doc)) return false;
    if (name.startsWith("D")) {
        StaticJsonDocument<1024> legacy;
        legacy.set(doc);
        doc.clear();
        doc["version"] = 1;
        doc["data"].set(legacy.as<JsonVariantConst>());
    }
    return !doc.overflowed() && doc["version"].is<int>() && !doc["version"].is<bool>() && doc["version"] == 1 &&
        doc["data"].is<JsonObject>() && doc["data"]["mac_address"].is<const char*>();
}

bool Queue::begin() {
    LittleFS.mkdir("/" + config_.folder);
    std::vector<String> temporary;
    Dir dir = LittleFS.openDir("/" + config_.folder);
    while (dir.next()) {
        String name = dir.fileName();
        name = name.substring(name.lastIndexOf('/') + 1);
        if (name.endsWith(".tmp") && (isRecord(name.substring(0, name.length() - 4)) || name == ".clock.json.tmp")) temporary.push_back(name);
    }
    for (const auto& name : temporary) {
        StaticJsonDocument<1536> doc;
        const bool valid = name == ".clock.json.tmp" ? readJson(path(name), doc, 512) &&
            doc["clock"].is<const char*>() && doc["offset"].is<int64_t>() : read(name, doc);
        if (valid) {
            if (!LittleFS.rename(path(name), path(name.substring(0, name.length() - 4)))) return false;
        } else if (!LittleFS.remove(path(name))) return false;
        yield();
    }
    return makeSpace(0, String(), false);
}

bool Queue::makeSpace(size_t bytes, const String& exclude, bool adding) {
    auto names = files();
    FSInfo info{};
    while (LittleFS.info(info)) {
        if (names.size() + (adding ? 1 : 0) <= config_.maxRecords &&
            info.totalBytes - info.usedBytes >= config_.reserveBytes + bytes + 2 * info.blockSize) return true;
        auto oldest = std::find_if(names.begin(), names.end(), [&](const String& name) { return name != exclude; });
        if (oldest == names.end() || !LittleFS.remove(path(*oldest))) return false;
        names.erase(oldest);
        Serial.println(F("Buffer full: discarded oldest measurement"));
        yield();
    }
    return false;
}

bool Queue::write(const String& name, const JsonDocument& doc, bool adding) {
    const size_t bytes = measureJson(doc);
    return bytes <= 2048 && makeSpace(bytes, name, adding) && writeJson(path(name), doc);
}

bool Queue::add(const JsonDocument& record) {
    uint64_t sequence = 0;
    for (const auto& name : files()) if (name.startsWith("q")) sequence = std::max(sequence, strtoull(name.c_str() + 1, nullptr, 10));
    if (sequence >= 999999999999999999ULL) return false;
    char name[32];
    snprintf(name, sizeof(name), "q%010llu.json", static_cast<unsigned long long>(sequence + 1));
    return write(String(name), record, true);
}

bool Queue::stamp(JsonDocument& record, JsonVariantConst map) {
    if (policy::parseTimestamp(record["data"]["timestamp"]) >= 0) return true;
    if (!map["clock"].is<const char*>() || !map["offset"].is<int64_t>() ||
        !record["clock"].is<const char*>() || !record["captured"].is<int64_t>() ||
        strcmp(record["clock"], map["clock"]) != 0) return false;
    String stamp = utcTimestamp(record["captured"].as<int64_t>() + map["offset"].as<int64_t>());
    if (stamp.length() == 0) return false;
    record["data"]["timestamp"] = stamp;
    return !record.overflowed();
}

bool Queue::rememberClock(StationClock& clock) {
    if (!clock.synced()) return true;
    StaticJsonDocument<256> previous;
    const bool hadMap = readJson(path(".clock.json"), previous, 512);
    if (hadMap && previous["clock"] == clock.session() && previous["offset"] == clock.offset()) return true;
    if (hadMap && previous["clock"] != clock.session()) {
        for (const auto& name : files()) {
            DynamicJsonDocument record(1536);
            if (read(name, record) && policy::parseTimestamp(record["data"]["timestamp"]) < 0 && stamp(record, previous.as<JsonVariantConst>())) {
                if (!write(name, record)) return false;
            }
            yield();
        }
    }
    StaticJsonDocument<256> current;
    current["clock"] = clock.session();
    current["offset"] = clock.offset();
    return write(".clock.json", current);
}

unsigned Queue::send(Api& api, StationClock& clock, uint32_t deadline) {
    if (!rememberClock(clock)) {
        Serial.println(F("Clock mapping could not be committed; queue retained"));
        return 0;
    }
    StaticJsonDocument<256> mapping;
    readJson(path(".clock.json"), mapping, 512);
    unsigned sent = 0, attempted = 0;
    for (const auto& name : files()) {
        yield();
        if (attempted >= config_.maxSend || WiFi.status() != WL_CONNECTED || static_cast<int32_t>(deadline - millis()) <= 0) break;
        DynamicJsonDocument record(1536);
        if (!read(name, record)) {
            Serial.println(F("Unreadable queue record retained for inspection/eviction"));
            continue;
        }
        if (policy::permanentRejection(record["rejected"] | 0)) continue;
        if (policy::parseTimestamp(record["data"]["timestamp"]) < 0) {
            if (!stamp(record, mapping.as<JsonVariantConst>())) {
                Serial.println(F("Queue time unknown; record retained"));
                continue;
            }
            if (!write(name, record)) break;
        }
        ++attempted;
        const int status = api.post(record["data"].as<JsonVariantConst>());
        if (policy::accepted(status)) {
            if (!LittleFS.remove(path(name))) {
                Serial.println(F("Server accepted measurement; removal failed, safe retry retained"));
                break;
            }
            ++sent;
            Serial.println(F("Queued measurement sent"));
        } else if (policy::permanentRejection(status)) {
            record["rejected"] = status;
            if (!write(name, record)) break;
            Serial.printf("Rejected queue record retained: HTTP %d\n", status);
        } else {
            Serial.printf("Queue upload stopped: %d; measurements retained\n", status);
            break;
        }
    }
    return sent;
}

void Queue::status() const {
    FSInfo info{};
    LittleFS.info(info);
    Serial.printf("Queued records: %u; flash free: %u bytes\n", static_cast<unsigned>(files().size()), static_cast<unsigned>(info.totalBytes - info.usedBytes));
}
