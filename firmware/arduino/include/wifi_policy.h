#pragma once
#include <ArduinoJson.h>
#include <stdint.h>
#include <string.h>
#include <initializer_list>

namespace wifi {
constexpr unsigned maxProfiles = 4;
struct Profile { const char* ssid; const char* password; };

inline uint32_t attemptTimeout(uint32_t perProfile, uint32_t remaining, unsigned pending) {
    const uint32_t fairShare = remaining / (pending ? pending : 1);
    return perProfile < fairShare ? perProfile : fairShare;
}

inline bool readProfile(JsonVariantConst value, Profile& out) {
    if (!value["SSID"].is<const char*>() || !value["PASSWD"].is<const char*>()) return false;
    const auto ssid = value["SSID"].as<JsonString>();
    const auto password = value["PASSWD"].as<JsonString>();
    out = {ssid.c_str(), password.c_str()};
    return ssid.size() >= 1 && ssid.size() <= 32 && password.size() <= 64 &&
        strlen(out.ssid) == ssid.size() && strlen(out.password) == password.size();
}

inline unsigned readProfiles(JsonVariantConst config, Profile (&out)[maxProfiles]) {
    if (!config.containsKey("WIFI_NETWORKS")) return readProfile(config, out[0]) ? 1 : 0;
    if (!config["WIFI_NETWORKS"].is<JsonArrayConst>()) return 0;
    const auto profiles = config["WIFI_NETWORKS"].as<JsonArrayConst>();
    if (profiles.size() < 1 || profiles.size() > maxProfiles) return 0;
    unsigned count = 0;
    for (JsonVariantConst value : profiles) {
        if (!value.is<JsonObjectConst>() || value.size() != 2 || !readProfile(value, out[count])) return 0;
        for (unsigned i = 0; i < count; ++i) if (!strcmp(out[i].ssid, out[count].ssid) &&
            !strcmp(out[i].password, out[count].password)) return 0;
        ++count;
    }
    return count;
}

inline uint32_t fingerprint(const Profile* profiles, unsigned count) {
    uint32_t hash = 2166136261U;
    for (unsigned i = 0; i < count; ++i) {
        for (const char* text : {profiles[i].ssid, profiles[i].password}) {
            for (const auto* p = reinterpret_cast<const uint8_t*>(text); *p; ++p) hash = (hash ^ *p) * 16777619U;
            hash *= 16777619U; // NUL delimiter; neither SSID nor password can contain NUL.
        }
    }
    return hash;
}

inline int cachedIndex(JsonVariantConst cache, uint32_t hash, unsigned count) {
    if (cache["version"] != 1 || !cache["fingerprint"].is<uint32_t>() || cache["fingerprint"].is<bool>() ||
        cache["fingerprint"].as<uint32_t>() != hash || !cache["index"].is<unsigned>() || cache["index"].is<bool>() ||
        cache["index"].as<unsigned>() >= count) return -1;
    return cache["index"].as<int>();
}

// Backend supplies bounded attempts and scanning. Prefer the last successful
// profile, then scan again and try visible profiles in configured order.
template <class Backend>
inline int connect(unsigned count, int preferred, Backend& backend) {
    if (count < 1 || count > maxProfiles) return -1;
    if (preferred < 0 || static_cast<unsigned>(preferred) >= count) preferred = -1;
    if (preferred >= 0 && backend.attempt(preferred)) return preferred;
    if (count == 1) return preferred < 0 && backend.attempt(0) ? 0 : -1;
    bool visible[maxProfiles]{};
    const bool scanned = backend.scan(visible);
    for (unsigned i = 0; i < count; ++i) {
        if (static_cast<int>(i) == preferred || (scanned && !visible[i])) continue;
        if (backend.attempt(i)) return i;
    }
    return -1;
}
} // namespace wifi
