#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace policy {
inline bool leap(int year) { return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0); }
inline int daysInMonth(int year, int month) {
    static const int days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    return month >= 1 && month <= 12 ? days[month - 1] + (month == 2 && leap(year)) : 0;
}
inline int64_t epoch(int year, int month, int day, int hour, int minute, int second) {
    if (year < 2020 || year > 2099 || month < 1 || month > 12 || day < 1 ||
        day > daysInMonth(year, month) || hour < 0 || hour > 23 ||
        minute < 0 || minute > 59 || second < 0 || second > 59) return -1;
    int64_t days = 0;
    for (int y = 1970; y < year; ++y) days += leap(y) ? 366 : 365;
    for (int m = 1; m < month; ++m) days += daysInMonth(year, m);
    return ((days + day - 1) * 24 + hour) * 3600 + minute * 60 + second;
}
inline int64_t parseTimestamp(const char* text) {
    if (!text) return -1;
    const size_t length = strlen(text);
    if ((length != 19 && length != 20) || (length == 20 && text[19] != 'Z')) return -1;
    if (text[4] != '-' || text[7] != '-' || text[10] != 'T' || text[13] != ':' || text[16] != ':') return -1;
    int parts[6] = {};
    const int starts[] = {0, 5, 8, 11, 14, 17};
    for (int part = 0; part < 6; ++part) {
        const int digits = part == 0 ? 4 : 2;
        for (int n = 0; n < digits; ++n) {
            const char c = text[starts[part] + n];
            if (c < '0' || c > '9') return -1;
            parts[part] = parts[part] * 10 + c - '0';
        }
    }
    return epoch(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]);
}
inline bool accepted(int status) { return status == 200 || status == 201; }
inline bool acknowledgesRecord(int status, const char* sent, const char* current) {
    return accepted(status) && sent && current && *current && strcmp(sent, current) == 0;
}
inline bool permanentRejection(int status) { return status == 400 || status == 409 || status == 422; }
inline uint32_t sleepRemainder(uint32_t interval, uint32_t awake) {
    return awake >= interval ? 1000 : interval - awake < 1000 ? 1000 : interval - awake;
}
inline uint32_t checksum(const void* bytes, size_t length) {
    const auto* p = static_cast<const uint8_t*>(bytes);
    uint32_t value = 2166136261U;
    while (length--) value = (value ^ *p++) * 16777619U;
    return value;
}
} // namespace policy
