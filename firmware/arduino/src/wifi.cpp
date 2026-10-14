#include "station.h"
#include "wifi_policy.h"
#include <algorithm>

namespace {
class Backend {
    const Config& config_;
    const uint32_t started_ = millis();
    const uint32_t budget_;
    unsigned pending_, attempted_ = 0;
    uint32_t remaining() const {
        const uint32_t elapsed = millis() - started_;
        return elapsed < budget_ ? budget_ - elapsed : 0;
    }
public:
    explicit Backend(const Config& config) : config_(config), budget_(std::min<uint32_t>(
        config.wifiTimeout * 1000 * config.networkCount + 5000, config.maxAwake * 500)), pending_(config.networkCount) {}

    bool attempt(unsigned index) {
        if (remaining() == 0) return false;
        const uint32_t timeout = wifi::attemptTimeout(config_.wifiTimeout * 1000, remaining(), pending_);
        if (pending_) --pending_;
        attempted_ |= 1U << index;
        WiFi.disconnect(false, false);
        Serial.printf("Wi-Fi trying profile %u/%u\n", index + 1, config_.networkCount);
        const uint32_t started = millis();
        WiFi.begin(config_.networks[index].ssid.c_str(), config_.networks[index].password.c_str());
        while (WiFi.status() != WL_CONNECTED && millis() - started < timeout) {
            const auto status = WiFi.status();
            if (millis() - started >= 500 && (status == WL_WRONG_PASSWORD || status == WL_NO_SSID_AVAIL || status == WL_CONNECT_FAILED)) break;
            delay(50);
        }
        if (WiFi.status() == WL_CONNECTED) return true;
        WiFi.disconnect(false, false);
        return false;
    }

    bool scan(bool (&visible)[wifi::maxProfiles]) {
        if (remaining() == 0) return false;
        Serial.println(F("Wi-Fi scanning configured networks"));
        WiFi.scanDelete();
        WiFi.scanNetworks(true);
        const uint32_t started = millis(), timeout = std::min<uint32_t>(5000, remaining());
        int found;
        while ((found = WiFi.scanComplete()) == WIFI_SCAN_RUNNING && millis() - started < timeout) delay(20);
        if (found < 0) { WiFi.scanDelete(); return false; }
        for (int n = 0; n < found; ++n) {
            const String ssid = WiFi.SSID(n);
            for (unsigned i = 0; i < config_.networkCount; ++i) if (ssid == config_.networks[i].ssid) visible[i] = true;
        }
        pending_ = 0;
        for (unsigned i = 0; i < config_.networkCount; ++i) if (visible[i] && !(attempted_ & (1U << i))) ++pending_;
        WiFi.scanDelete();
        return true;
    }
};
}

bool connectStationWifi(const Config& config) {
    wifi::Profile profiles[wifi::maxProfiles];
    for (unsigned i = 0; i < config.networkCount; ++i) profiles[i] = {
        config.networks[i].ssid.c_str(), config.networks[i].password.c_str()};
    const uint32_t hash = wifi::fingerprint(profiles, config.networkCount);
    StaticJsonDocument<256> cache;
    const int preferred = readJson("/.wifi.json", cache, 256) ? wifi::cachedIndex(cache.as<JsonVariantConst>(), hash, config.networkCount) : -1;
    int selected = -1;
    if (WiFi.status() == WL_CONNECTED) {
        for (unsigned i = 0; i < config.networkCount; ++i) if (WiFi.SSID() == config.networks[i].ssid) {
            selected = i;
            if (static_cast<int>(i) == preferred) break;
        }
    }
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);
    if (selected < 0) {
        WiFi.disconnect(false, false);
        Backend backend(config);
        selected = wifi::connect(config.networkCount, preferred, backend);
    }
    if (selected < 0) {
        Serial.println(F("Wi-Fi unavailable; queued measurements retained; discovery will retry next cycle"));
        return false;
    }
    if (selected != preferred) {
        cache.clear();
        cache["version"] = 1;
        cache["fingerprint"] = hash;
        cache["index"] = selected;
        if (!writeJson("/.wifi.json", cache)) Serial.println(F("Wi-Fi preference could not be saved; next boot will discover again"));
    }
    Serial.printf("Wi-Fi connected; profile %u/%u; free heap: %u\n", selected + 1, config.networkCount, ESP.getFreeHeap());
    return true;
}
