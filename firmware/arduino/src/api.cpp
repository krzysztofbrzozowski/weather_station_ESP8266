#include "station.h"
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecureBearSSL.h>
#include <time.h>
#include <memory>

namespace {
// HTTPClient decodes chunked bodies into this bounded sink. An oversized error
// page must not exhaust the heap that BearSSL shares with the application.
class ResponseBuffer : public Stream {
public:
    char data[2049]{};
    size_t length = 0;
    bool overflow = false;
    using Print::write;
    size_t write(uint8_t c) override { return write(&c, 1); }
    size_t write(const uint8_t* bytes, size_t size) override {
        if (length + size > sizeof(data) - 1) { overflow = true; return 0; }
        memcpy(data + length, bytes, size);
        length += size;
        data[length] = '\0';
        return size;
    }
    int available() override { return 0; }
    int availableForWrite() override { return sizeof(data) - 1 - length; }
    int read() override { return -1; }
    int peek() override { return -1; }
    void flush() override {}
};
}

int Api::request(bool post, JsonVariantConst data, JsonDocument* reply) {
    if (WiFi.status() != WL_CONNECTED) return -100;
    if (time(nullptr) < policy::epoch(2020, 1, 1, 0, 0, 0)) {
        Serial.println(F("HTTPS deferred: current UTC time is required to validate certificates"));
        return -101;
    }
    File ca = LittleFS.open(config_.caFile, "r");
    if (!ca || ca.size() == 0 || ca.size() > 8192) {
        Serial.println(F("HTTPS deferred: missing/oversized CA bundle"));
        return -102;
    }
    String pem = ca.readString();
    ca.close();
    BearSSL::X509List anchors(pem.c_str());
    pem = String();
    if (anchors.getCount() == 0) {
        Serial.println(F("HTTPS deferred: invalid CA bundle"));
        return -102;
    }
    BearSSL::WiFiClientSecure client;
    client.setTrustAnchors(&anchors);
    client.setX509Time(time(nullptr));
    client.setSSLVersion(BR_TLS12, BR_TLS12);
    // Full receive buffer: do not assume Cloudflare supports smaller fragments.
    client.setBufferSizes(16384, 512);
    // The pinned ESP8266 core gives BearSSL a 15-second handshake timeout.
    client.setTimeout(config_.timeout * 1000);
    HTTPClient http;
    http.setReuse(false);
    http.setTimeout(config_.timeout * 1000);
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    http.setUserAgent("weather-station-esp8266/1.0");
    const String url = config_.apiUrl + (post ? "" : "/timesync");
    if (!http.begin(client, url)) return -103;
    int status;
    if (post) {
        String payload;
        if (serializeJson(data, payload) == 0 || payload.length() > 2048) { http.end(); return -104; }
        http.addHeader("Content-Type", "application/json");
        http.addHeader("Authorization", "Token " + config_.token);
        status = http.POST(payload);
    } else status = http.GET();
    if (status < 0) {
        char error[160]{};
        const int tls = client.getLastSSLError(error, sizeof(error));
        Serial.printf("HTTPS transport error: %d (%s); TLS %d: %s\n", status,
            HTTPClient::errorToString(status).c_str(), tls, error);
    } else {
        Serial.printf("HTTPS %s: HTTP %d; free heap: %u\n", post ? "POST" : "GET", status, ESP.getFreeHeap());
        if (reply && status == 200) {
            std::unique_ptr<ResponseBuffer> body(new (std::nothrow) ResponseBuffer());
            const int declared = http.getSize();
            const int transferred = body && declared <= 2048 ? http.writeToStream(body.get()) : -1;
            if (!body || declared > 2048 || transferred < 0 || body->overflow) {
                Serial.printf("API response read failed: declared=%d, transferred=%d\n", declared, transferred);
                status = -105;
            } else {
                // Const input copies JSON keys into reply; mutable input would
                // leave dangling pointers when the bounded body is freed.
                auto error = deserializeJson(*reply, static_cast<const char*>(body->data), body->length,
                                             DeserializationOption::NestingLimit(4));
                if (error) { Serial.printf("API response JSON failed: %s\n", error.c_str()); status = -105; }
            }
        }
    }
    http.end();
    client.stop();
    return status;
}

int Api::getTime(int64_t& epoch) {
    StaticJsonDocument<512> reply;
    int status = request(false, JsonVariantConst(), &reply);
    if (status != 200) return status;
    JsonArrayConst values = reply["datetime_now"].as<JsonArrayConst>();
    if (values.size() != 8) return -106;
    for (auto value : values) if (!value.is<int>() || value.is<bool>()) return -106;
    epoch = policy::epoch(values[0], values[1], values[2], values[4], values[5], values[6]);
    return epoch < 0 ? -106 : status;
}

int Api::post(JsonVariantConst data) { return request(true, data, nullptr); }
