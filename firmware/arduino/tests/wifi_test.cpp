#include "wifi_policy.h"
#include "auth_config.h"
#include <cassert>
#include <sstream>
#include <vector>

struct Backend {
    bool visible[4]{}, success[4]{}, scanOk = true;
    unsigned scans = 0;
    std::vector<unsigned> attempts;
    bool attempt(unsigned index) { attempts.push_back(index); return success[index]; }
    bool scan(bool (&out)[4]) {
        ++scans;
        for (unsigned i = 0; i < 4; ++i) out[i] = visible[i];
        return scanOk;
    }
};

int main() {
    assert(wifi::attemptTimeout(30000, 45000, 4) == 11250);
    assert(wifi::attemptTimeout(12000, 40000, 1) == 12000);
    assert(wifi::attemptTimeout(12000, 0, 4) == 0);
    {
        Backend b; b.success[2] = true;
        assert(wifi::connect(4, 2, b) == 2 && b.scans == 0);
        assert(b.attempts == std::vector<unsigned>{2});
    }
    {
        Backend b; b.visible[0] = b.visible[1] = true; b.success[1] = true;
        assert(wifi::connect(4, 2, b) == 1 && b.scans == 1);
        assert((b.attempts == std::vector<unsigned>{2, 0, 1}));
    }
    {
        Backend b; b.visible[3] = b.success[3] = true;
        assert(wifi::connect(4, -1, b) == 3 && b.scans == 1);
        assert(b.attempts == std::vector<unsigned>{3});
    }
    {
        Backend b; b.visible[0] = b.visible[2] = b.success[2] = true;
        assert(wifi::connect(4, -1, b) == 2);
        assert((b.attempts == std::vector<unsigned>{0, 2}));
    }
    {
        Backend b;
        assert(wifi::connect(4, -1, b) == -1 && b.scans == 1 && b.attempts.empty());
    }
    {
        Backend b; b.scanOk = false; b.success[3] = true;
        assert(wifi::connect(4, 99, b) == 3 && b.scans == 1);
        assert((b.attempts == std::vector<unsigned>{0, 1, 2, 3}));
    }
    {
        Backend b; b.success[0] = true;
        assert(wifi::connect(1, -1, b) == 0 && b.scans == 0);
        assert(wifi::connect(0, -1, b) == -1 && wifi::connect(5, -1, b) == -1);
    }
    DynamicJsonDocument doc(4096);
    assert(!deserializeJson(doc, R"({"WIFI_NETWORKS":[{"SSID":"home","PASSWD":"unit-a"},{"SSID":"office","PASSWD":"unit-b"},{"SSID":"open","PASSWD":""},{"SSID":"home","PASSWD":"unit-other"}]})"));
    wifi::Profile profiles[4];
    assert(wifi::readProfiles(doc.as<JsonVariantConst>(), profiles) == 4);
    const auto hash = wifi::fingerprint(profiles, 4);
    StaticJsonDocument<256> cache;
    cache["version"] = 1; cache["index"] = 3; cache["fingerprint"] = hash;
    assert(wifi::cachedIndex(cache.as<JsonVariantConst>(), hash, 4) == 3);
    assert(wifi::cachedIndex(cache.as<JsonVariantConst>(), hash + 1, 4) == -1);
    cache["index"] = 4;
    assert(wifi::cachedIndex(cache.as<JsonVariantConst>(), hash, 4) == -1);
    cache["index"] = true;
    assert(wifi::cachedIndex(cache.as<JsonVariantConst>(), hash, 4) == -1);
    doc["WIFI_NETWORKS"][0]["PASSWD"] = "unit-changed";
    assert(wifi::readProfiles(doc.as<JsonVariantConst>(), profiles) == 4);
    assert(wifi::fingerprint(profiles, 4) != hash);
    for (const char* raw : {R"({"WIFI_NETWORKS":[]})", R"({"WIFI_NETWORKS":{}})",
            R"({"WIFI_NETWORKS":[{"SSID":"x","PASSWD":true}]})",
            R"({"WIFI_NETWORKS":[{"SSID":"","PASSWD":""}]})",
            R"({"WIFI_NETWORKS":[{"SSID":"x","PASSWD":"a"},{"SSID":"x","PASSWD":"a"}]})",
            R"({"WIFI_NETWORKS":[{"SSID":"bad\u0000ssid","PASSWD":"a"}]})"}) {
        doc.clear(); assert(!deserializeJson(doc, raw));
        assert(wifi::readProfiles(doc.as<JsonVariantConst>(), profiles) == 0);
    }
    doc.clear(); doc["SSID"] = "legacy"; doc["PASSWD"] = "";
    assert(wifi::readProfiles(doc.as<JsonVariantConst>(), profiles) == 1);
    std::istringstream input(R"({"API_url":"https://example.com","AUTH_TOKEN":"unit-token","WIFI_NETWORKS":[{"SSID":"home","PASSWD":"unit-password"}]})");
    assert(auth::readConfig(doc, input, "02:00:00:00:00:01"));
    assert(wifi::readProfiles(doc.as<JsonVariantConst>(), profiles) == 1 && !strcmp(profiles[0].ssid, "home"));
}
