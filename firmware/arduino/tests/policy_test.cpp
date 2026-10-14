#include "record_policy.h"
#include <cassert>
#include <cstring>
#include <initializer_list>

int main() {
    using namespace policy;
    assert(parseTimestamp("2024-02-29T12:00:00Z") == 1709208000LL);
    assert(parseTimestamp("2026-02-29T12:00:00Z") == -1);
    assert(parseTimestamp("2026-10-14T13:00:00Z") == epoch(2026, 10, 14, 13, 0, 0));
    assert(parseTimestamp("2026-10-14T13:00:00") == epoch(2026, 10, 14, 13, 0, 0));
    for (const char* bad : {"2026-13-01T00:00:00Z", "2026-01-00T00:00:00Z", "2026-01-01T24:00:00Z",
            "2026-01-01T00:60:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:00+01:00",
            "2026-01-01T00:00:00X", "2026-01-01T00:00:00Zextra", "202x-01-01T00:00:00Z", ""}) assert(parseTimestamp(bad) == -1);
    assert(parseTimestamp(nullptr) == -1);
    assert(accepted(200) && accepted(201));
    for (int code : {202, 204, 301, 400, 401, 409, 422, 429, 500, -40, -1}) assert(!accepted(code));
    assert(permanentRejection(400) && permanentRejection(409) && permanentRejection(422));
    for (int code : {200, 201, 401, 403, 429, 500, -1}) assert(!permanentRejection(code));
    assert(sleepRemainder(300000, 3000) == 297000);
    assert(sleepRemainder(300000, 300000) == 1000);
    assert(sleepRemainder(10000, 12000) == 1000);
    assert(sleepRemainder(10000, 9500) == 1000);
    const uint32_t started = 0xfffffff0U, now = 0x00000020U;
    assert(sleepRemainder(10000, now - started) == 9952);
    char bytes[] = "persistent RTC state";
    const uint32_t original = checksum(bytes, sizeof(bytes));
    bytes[3] ^= 1;
    assert(checksum(bytes, sizeof(bytes)) != original);
}
