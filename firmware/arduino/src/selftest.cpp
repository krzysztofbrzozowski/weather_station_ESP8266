#include "station.h"

bool queueSelfTest(const Config& config) {
    const String directory = "/_queue_check";
    if (LittleFS.exists(directory)) {
        Serial.println(F("Queue selftest refused: diagnostic directory already exists"));
        return false;
    }
    const auto productionFiles = Queue(config).files();
    Config test = config;
    test.folder = "_queue_check";
    test.maxRecords = 3;
    test.reserveBytes = 8192;
    Queue queue(test);
    bool ok = queue.begin();
    DynamicJsonDocument record(512);
    record["version"] = 1;
    record["data"]["mac_address"] = "diagnostic-not-for-upload";
    record["data"]["timestamp"] = "2026-01-01T00:00:00Z";
    for (unsigned n = 1; ok && n <= 4; ++n) {
        record["data"]["temperature"] = n;
        ok = queue.add(record);
    }
    auto files = queue.files();
    ok = ok && files.size() == 3 && files.front() == "q0000000002.json" && files.back() == "q0000000004.json";
    if (!ok) Serial.println(F("Queue selftest: initial circular eviction failed"));
    if (ok) {
        // A complete temporary record is committed on recovery; an incomplete
        // one is removed. Neither should corrupt the surrounding queue.
        ok = writeJson(directory + "/q0000000005.json.tmp", record);
        File broken = LittleFS.open(directory + "/q0000000006.json.tmp", "w");
        ok = ok && broken && broken.print("{\"version\":") == 11;
        broken.close();
        ok = ok && queue.begin();
        files = queue.files();
        ok = ok && files.size() == 3 && files.front() == "q0000000003.json" && files.back() == "q0000000005.json" &&
            !LittleFS.exists(directory + "/q0000000006.json.tmp");
        if (!ok) {
            Serial.println(F("Queue selftest: interrupted-write recovery failed"));
            for (const auto& name : files) Serial.println(name);
        }
    }
    ok = ok && Queue(config).files() == productionFiles;
    std::vector<String> cleanup;
    {
        Dir dir = LittleFS.openDir(directory);
        while (dir.next()) cleanup.push_back(dir.fileName());
    }
    for (String name : cleanup) {
        name = name.substring(name.lastIndexOf('/') + 1);
        if (!LittleFS.remove(directory + "/" + name)) {
            Serial.printf("Queue selftest: cleanup failed for %s\n", name.c_str());
            ok = false;
        }
    }
    // ESP8266 LittleFS.remove() also prunes empty parent directories.
    if (LittleFS.exists(directory) && !LittleFS.rmdir(directory)) {
        Serial.println(F("Queue selftest: diagnostic directory cleanup failed"));
        ok = false;
    }
    Serial.println(ok ? F("Queue selftest PASS: circular eviction and interrupted-write recovery; no diagnostic readings uploaded") :
                        F("Queue selftest FAILED; production queue was not used for uploads"));
    return ok;
}
