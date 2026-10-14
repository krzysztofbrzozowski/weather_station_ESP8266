#pragma once
#include <ArduinoJson.h>
#include <string.h>
#include <initializer_list>

namespace auth {
constexpr unsigned maxDevices = 100;
constexpr size_t maxConfigBytes = 32768;
constexpr size_t configCapacity = 4096;
inline bool normalizeMac(const char* value, char (&out)[18]) {
    if (!value || strlen(value) != 17) return false;
    for (unsigned i = 0; i < 17; ++i) {
        char c = value[i];
        if (i % 3 == 2) {
            if (c != ':' && c != '-') return false;
            out[i] = ':';
        } else {
            if (c >= 'A' && c <= 'F') c += 'a' - 'A';
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
            out[i] = c;
        }
    }
    out[17] = '\0';
    return true;
}

inline bool validToken(const char* token) {
    if (!token || !*token || strlen(token) > 256) return false;
    for (const char* p = token; *p; ++p) if (*p < 33 || *p > 126) return false;
    return true;
}

struct Selection { bool valid; const char* token; };

// Returned token belongs to the document. Null means an unknown device;
// a present map is authoritative, even when AUTH_TOKEN is also provided.
inline Selection select(JsonVariantConst config, const char* deviceMac) {
    if (!config.containsKey("AUTH_TOKENS")) {
        const char* token = config["AUTH_TOKEN"].as<const char*>();
        return {validToken(token), token};
    }
    if (!config["AUTH_TOKENS"].is<JsonObjectConst>()) return {false, nullptr};
    JsonObjectConst tokens = config["AUTH_TOKENS"].as<JsonObjectConst>();
    if (tokens.size() == 0 || tokens.size() > maxDevices) return {false, nullptr};
    char device[18];
    if (!normalizeMac(deviceMac, device)) return {false, nullptr};
    const char* selected = nullptr;
    for (JsonPairConst entry : tokens) {
        char normalized[18];
        if (!normalizeMac(entry.key().c_str(), normalized)) return {false, nullptr};
        for (JsonPairConst previous : tokens) {
            if (previous.key().c_str() == entry.key().c_str()) break;
            char earlier[18];
            if (!normalizeMac(previous.key().c_str(), earlier) || !strcmp(earlier, normalized)) return {false, nullptr};
        }
        const char* token = entry.value().as<const char*>();
        if (!validToken(token)) return {false, nullptr};
        if (!strcmp(device, normalized)) selected = token;
    }
    return {true, selected};
}

// prepare.py validates and canonicalizes the complete registry on the host.
// Stream past other devices' tokens instead of allocating the registry in RAM.
template <typename Input>
inline bool readConfig(JsonDocument& doc, Input& input, const char* deviceMac) {
    char mac[18];
    if (!normalizeMac(deviceMac, mac)) return false;
    StaticJsonDocument<768> filter;
    for (const char* key : {"API_url", "AUTH_TOKEN", "AUTH_TOKENS_COUNT", "SSID", "PASSWD", "WIFI_NETWORKS", "BACKUP_FOLDER",
            "MEASUREMENT_INTERVAL_S", "HTTP_TIMEOUT_S", "BACKUP_MAX_RECORDS", "BACKUP_MIN_FREE_BYTES",
            "BACKUP_MAX_SEND", "WIFI_TIMEOUT_S", "NTP_TIMEOUT_S", "MAX_AWAKE_S", "NTP_SERVER", "CA_FILE"}) filter[key] = true;
    filter["AUTH_TOKENS"][mac] = true;
    return !filter.overflowed() && !deserializeJson(doc, input, DeserializationOption::Filter(filter),
                                                  DeserializationOption::NestingLimit(6));
}

inline Selection selectFiltered(JsonVariantConst config, const char* deviceMac) {
    if (config.containsKey("AUTH_TOKENS")) {
        if (config.containsKey("AUTH_TOKENS_COUNT") &&
            (!config["AUTH_TOKENS_COUNT"].is<unsigned>() || config["AUTH_TOKENS_COUNT"].is<bool>() ||
            config["AUTH_TOKENS_COUNT"].as<unsigned>() < 1 || config["AUTH_TOKENS_COUNT"].as<unsigned>() > maxDevices ||
            config["AUTH_TOKENS_COUNT"].as<unsigned>() < config["AUTH_TOKENS"].size())) return {false, nullptr};
        if (!config["AUTH_TOKENS"].is<JsonObjectConst>() || config["AUTH_TOKENS"].size() > 1) return {false, nullptr};
        if (config["AUTH_TOKENS"].size() == 0) return {true, nullptr};
    }
    return select(config, deviceMac);
}
} // namespace auth
