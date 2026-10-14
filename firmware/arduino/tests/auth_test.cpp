#include "auth_config.h"
#include <cassert>
#include <cstring>
#include <cstdio>
#include <initializer_list>
#include <sstream>
#include <string>
#include <iostream>

int main() {
    DynamicJsonDocument doc(8192);
    assert(!deserializeJson(doc, R"({"AUTH_TOKEN":"unit-legacy","AUTH_TOKENS":{"DC-4F-22-7C-3A-FA":"unit-first","dc:4f:22:7c:3a:fc":"unit-second"}})"));
    auto selected = auth::select(doc.as<JsonVariantConst>(), "dc:4f:22:7c:3a:fa");
    assert(selected.valid && !strcmp(selected.token, "unit-first"));
    selected = auth::select(doc.as<JsonVariantConst>(), "DC:4F:22:7C:3A:FC");
    assert(selected.valid && !strcmp(selected.token, "unit-second"));
    selected = auth::select(doc.as<JsonVariantConst>(), "dc:4f:22:7c:3c:0d");
    assert(selected.valid && selected.token == nullptr); // Never use the legacy fallback.
    for (const char* json : {
        R"({"AUTH_TOKENS":{}})", R"({"AUTH_TOKENS":[]})", R"({"AUTH_TOKENS":null})", R"({"AUTH_TOKENS":"unit-secret"})",
        R"({"AUTH_TOKENS":{"invalid":"unit-token"}})",
        R"({"AUTH_TOKENS":{"dc:4f:22:7c:3a:fa":true}})",
        R"({"AUTH_TOKENS":{"dc:4f:22:7c:3a:fa":""}})",
        R"({"AUTH_TOKENS":{"dc:4f:22:7c:3a:fa":"unit\r\nInjected"}})",
        R"({"AUTH_TOKENS":{"dc:4f:22:7c:3a:fa":"unit-first","DC-4F-22-7C-3A-FA":"unit-other"}})",
        R"({})"}) {
        doc.clear();
        assert(!deserializeJson(doc, json));
        assert(!auth::select(doc.as<JsonVariantConst>(), "dc:4f:22:7c:3a:fa").valid);
    }
    doc.clear();
    doc["AUTH_TOKEN"] = "unit-legacy";
    selected = auth::select(doc.as<JsonVariantConst>(), "dc:4f:22:7c:3a:fa");
    assert(selected.valid && !strcmp(selected.token, "unit-legacy"));
    doc.clear();
    for (unsigned n = 0; n < 101; ++n) {
        char mac[18];
        snprintf(mac, sizeof(mac), "02:00:00:00:00:%02x", n);
        doc["AUTH_TOKENS"][mac] = "unit-token";
    }
    assert(!auth::select(doc.as<JsonVariantConst>(), "02:00:00:00:00:00").valid);

    // The production reader must handle a worst-case registry using the same
    // small document as the ESP8266, even when the matching key is last.
    DynamicJsonDocument registry(65536);
    registry["API_url"] = "https://example.com/api/measurements";
    registry["SSID"] = "unit-wifi";
    registry["PASSWD"] = "unit-password";
    registry["AUTH_TOKEN"] = "unit-do-not-use";
    registry["AUTH_TOKENS_COUNT"] = 100;
    registry["MEASUREMENT_INTERVAL_S"] = 1800;
    registry["BACKUP_MAX_RECORDS"] = 144;
    registry["CA_FILE"] = "/ca.pem";
    for (unsigned n = 0; n < 4; ++n) {
        auto profile = registry["WIFI_NETWORKS"].createNestedObject();
        profile["SSID"] = std::string(31, 's') + char('0' + n);
        profile["PASSWD"] = std::string(64, 'p');
    }
    for (unsigned n = 0; n < 100; ++n) {
        char mac[18];
        snprintf(mac, sizeof(mac), "02:00:00:00:00:%02x", n);
        std::string token(256, 'x');
        token.replace(0, 17, mac);
        registry["AUTH_TOKENS"][mac] = token;
    }
    assert(!registry.overflowed());
    std::string serialized;
    serializeJson(registry, serialized);
    assert(serialized.size() > 25000 && serialized.size() < auth::maxConfigBytes);
    size_t peak = 0;
    for (unsigned n : {0U, 50U, 99U, 100U}) {
        char mac[18];
        snprintf(mac, sizeof(mac), "02:00:00:00:00:%02x", n);
        std::istringstream input(serialized);
        DynamicJsonDocument filtered(auth::configCapacity);
        assert(auth::readConfig(filtered, input, mac));
        peak = std::max(peak, filtered.memoryUsage());
        assert(filtered.memoryUsage() < 2048 && !filtered.overflowed());
        assert(filtered["MEASUREMENT_INTERVAL_S"] == 1800);
        assert(filtered["CA_FILE"] == "/ca.pem");
        assert(filtered["WIFI_NETWORKS"].size() == 4);
        assert(filtered["AUTH_TOKENS"].size() == (n < 100 ? 1U : 0U));
        selected = auth::selectFiltered(filtered.as<JsonVariantConst>(), mac);
        assert(selected.valid);
        if (n < 100) assert(selected.token && strlen(selected.token) == 256 && !strncmp(selected.token, mac, 17));
        else assert(selected.token == nullptr);
        // Previously staged maps (without count metadata) still work.
        filtered.remove("AUTH_TOKENS_COUNT");
        assert(auth::selectFiltered(filtered.as<JsonVariantConst>(), mac).valid);
        filtered["AUTH_TOKENS_COUNT"] = 101;
        assert(!auth::selectFiltered(filtered.as<JsonVariantConst>(), mac).valid);
    }
    std::cout << "PASS: 100 x 256-character tokens; filtered JSON memory <= " << peak << " bytes on host\n";
}
