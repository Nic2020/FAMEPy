/* SPDX-License-Identifier: MIT
 * Independent ABI-mechanics fixture, not an implementation of the vendor library.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

/* Synthetic values, not vendor sentinel encodings. Exercise discovery only. */
API double FPRCNA = -1.0;
API const char *FSTRNA = "synthetic-na";
API char FSTRNC[16] = "synthetic-nc";

typedef struct {
    int32_t frequency;
    int64_t start;
    int64_t end;
} TestRange;

API int32_t shim_range_size(void) { return (int32_t)sizeof(TestRange); }
API int32_t shim_start_offset(void) { return (int32_t)offsetof(TestRange, start); }
API int32_t shim_end_offset(void) { return (int32_t)offsetof(TestRange, end); }

API void cfmver(int32_t *status, float *version) {
    *status = 0;
    *version = 4.25f;
}

API void cfmopdb(int32_t *status, int32_t *key, const char *name, int32_t mode) {
    (void)name;
    *status = mode == 1 ? 0 : 67;
    *key = 42;
}

API int32_t fame_year_period_to_index(int32_t frequency, int64_t *index,
                                    int32_t year, int32_t period) {
    (void)frequency;
    *index = ((int64_t)year << 32) + period;
    return 0;
}

API int32_t fame_get_precisions(int32_t key, const char *name,
                               const TestRange *range, double *values) {
    int64_t i;
    (void)key;
    (void)name;
    if (range == NULL) {
        values[0] = 12.5;
        return 0;
    }
    if (range->end < range->start || range->end - range->start > 8) return 67;
    for (i = 0; i <= range->end - range->start; ++i)
        values[i] = (double)(range->start + i) + 0.5;
    return 0;
}

API int32_t fame_get_strings(int32_t key, const char *name, const TestRange *range,
                            char **values, int32_t *lengths, int32_t *missing) {
    (void)key;
    (void)name;
    (void)range;
    (void)missing;
    if (lengths[0] < 3 || lengths[1] < 2) return 67;
    memcpy(values[0], "abc", 4);
    memcpy(values[1], "xy", 3);
    lengths[0] = 3;
    lengths[1] = 2;
    return 0;
}
