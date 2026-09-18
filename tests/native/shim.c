/* SPDX-License-Identifier: MIT
 * Independent ABI-mechanics fixture. It implements the candidate declarations
 * with an in-memory toy database so that pointer, buffer, lifetime and error
 * contracts of the Python binding can be exercised without vendor code. It is
 * not an implementation of the vendor library and proves nothing about FAME.
 *
 * Synthetic statuses (also used by the Python fake backend):
 *   901 not initialized      902 already initialized   903 bad key
 *   904 read-only database   905 object/database exists 906 missing database
 *   907 type mismatch        908 bad range             909 bad wildcard
 *   910 bad mode             911 name too long
 * Reference-derived statuses: 0 success, 3 already finished, 13 no object, 18 truncated, 67 bad
 * option, 513 command error (extended text available).
 *
 * Behaviors observed on both protected hosts and modeled on purpose: the string
 * missing sentinels are two bytes that are not ASCII text; an ITEM FREQUENCY
 * selection is accepted but does not narrow the wildcard; "display" output goes
 * to the C-level stdout while no redirection is active; the redirection creates
 * its output file. Endpoint handling of missing observations and the write /
 * direct-write prerequisites are not established, so the shim stores what it is
 * given and accepts every mode on an existing store.
 */
#define _CRT_SECURE_NO_WARNINGS
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

#define S_NOT_INITIALIZED 901
#define S_ALREADY_INITIALIZED 902
#define S_BAD_KEY 903
#define S_READONLY 904
#define S_EXISTS 905
#define S_MISSING 906
#define S_TYPE_MISMATCH 907
#define S_RANGE 908
#define S_BAD_WILDCARD 909
#define S_BAD_MODE 910
#define S_NAME_TOO_LONG 911
#define HFIN 3
#define HNOOBJ 13
#define HTRUNC 18
#define HBOPT 67
#define HFAMER 513

#define MAX_DB 8
#define MAX_STORE 8
#define MAX_OBJ 32
#define MAX_NAME 260
#define MAX_VALUES 64
#define MAX_STRING 64
#define MAX_CURSORS 4

typedef struct {
    int32_t frequency;
    int64_t start;
    int64_t end;
} TestRange;

/* Synthetic sentinel globals with distinct NaN payloads; not vendor values. */
API int64_t FAME_INDEX_NC = -4611686018427387905LL;
API int64_t FAME_INDEX_NA = -4611686018427387906LL;
API int64_t FAME_INDEX_ND = -4611686018427387907LL;
API double FPRCNC, FPRCNA, FPRCND;
API float FNUMNC, FNUMNA, FNUMND;
API int32_t FBOONC = -2147483647, FBOONA = -2147483646, FBOOND = -2147483645;
/* Two-byte non-ASCII string sentinels (synthetic, not vendor values). */
API char FSTRNC[3] = "\xfe\x01";
API char FSTRNA[3] = "\xfe\x02";
API char FSTRND[3] = "\xfe\x03";

typedef struct {
    int used;
    char name[MAX_NAME];
    int32_t cls, type, freq, basis, observed;
    int64_t first, last;
    int has_data;
    int count;
    double d[MAX_VALUES];
    float f[MAX_VALUES];
    int32_t b[MAX_VALUES];
    int64_t i[MAX_VALUES];
    char s[MAX_VALUES][MAX_STRING];
    char namelist[256];
} Object;

typedef struct {
    int used;
    char name[512];
    Object objects[MAX_OBJ];
} Store;

typedef struct {
    int used;
    int mode;
    int is_work;
    char name[512];
    Object objects[MAX_OBJ];
} Handle;

typedef struct {
    int used;
    int position;
    int count;
    int object_index[MAX_OBJ];
    int db;
} Cursor;

static int initialized = 0;
static int init_count = 0;
static int fin_count = 0;
static int fail_next = 0;
static Store stores[MAX_STORE];
static Handle handles[MAX_DB];
static Cursor cursors[MAX_CURSORS];
static Object work_objects[MAX_OBJ];
static FILE *output = NULL;
static char error_text[256] = "";
static int option_all[4] = {1, 1, 1, 1}; /* CLASS, TYPE, FREQUENCY, ALIAS */
static char option_values[4][8][32];
static int option_counts[4];

static void init_globals(void) {
    uint64_t q;
    uint32_t w;
    q = 0x7FF8000000000101ULL; memcpy(&FPRCNC, &q, 8);
    q = 0x7FF8000000000102ULL; memcpy(&FPRCNA, &q, 8);
    q = 0x7FF8000000000103ULL; memcpy(&FPRCND, &q, 8);
    w = 0x7FC00101u; memcpy(&FNUMNC, &w, 4);
    w = 0x7FC00102u; memcpy(&FNUMNA, &w, 4);
    w = 0x7FC00103u; memcpy(&FNUMND, &w, 4);
}

static int string_missing(const char *value) {
    return strcmp(value, FSTRNC) == 0 ? 1 : strcmp(value, FSTRNA) == 0 ? 2
           : strcmp(value, FSTRND) == 0 ? 3 : 0;
}

static int globals_ready = 0;
#define PREPARE() do { if (!globals_ready) { init_globals(); globals_ready = 1; } } while (0)

/* Fault injection consumed by the next entered function. */
#define CFM_ENTER(st) do { PREPARE(); if (fail_next) { *(st) = fail_next; fail_next = 0; return; } } while (0)
#define FAME_ENTER() do { PREPARE(); if (fail_next) { int32_t s_ = fail_next; fail_next = 0; return s_; } } while (0)

API int32_t shim_range_size(void) { return (int32_t)sizeof(TestRange); }
API int32_t shim_start_offset(void) { return (int32_t)offsetof(TestRange, start); }
API int32_t shim_end_offset(void) { return (int32_t)offsetof(TestRange, end); }
API void shim_fail_next(int32_t status) { fail_next = status; }
API int32_t shim_initialized(void) { return initialized; }
API int32_t shim_init_count(void) { return init_count; }
API int32_t shim_fin_count(void) { return fin_count; }
API int32_t shim_open_databases(void) {
    int n = 0, k;
    for (k = 0; k < MAX_DB; ++k) n += handles[k].used;
    return n;
}
API int32_t shim_active_cursors(void) {
    int n = 0, k;
    for (k = 0; k < MAX_CURSORS; ++k) n += cursors[k].used;
    return n;
}
API int32_t shim_output_redirected(void) { return output != NULL; }
API void shim_reset(void) {
    if (output) fclose(output);
    output = NULL;
    initialized = 0;
    init_count = fin_count = fail_next = 0;
    memset(stores, 0, sizeof stores);
    memset(handles, 0, sizeof handles);
    memset(cursors, 0, sizeof cursors);
    memset(work_objects, 0, sizeof work_objects);
    error_text[0] = '\0';
    option_all[0] = option_all[1] = option_all[2] = option_all[3] = 1;
    memset(option_counts, 0, sizeof option_counts);
}

static void upper_copy(char *dst, const char *src, size_t cap) {
    size_t n = 0;
    while (src[n] && n + 1 < cap) { dst[n] = (char)toupper((unsigned char)src[n]); ++n; }
    dst[n] = '\0';
}

static Handle *handle_of(int32_t key) {
    if (key < 0 || key >= MAX_DB || !handles[key].used) return NULL;
    return &handles[key];
}

static Object *find_object(Handle *h, const char *name) {
    char upper[MAX_NAME];
    int k;
    upper_copy(upper, name, sizeof upper);
    for (k = 0; k < MAX_OBJ; ++k)
        if (h->objects[k].used && strcmp(h->objects[k].name, upper) == 0) return &h->objects[k];
    return NULL;
}

static Store *find_store(const char *name) {
    int k;
    for (k = 0; k < MAX_STORE; ++k)
        if (stores[k].used && strcmp(stores[k].name, name) == 0) return &stores[k];
    return NULL;
}

/* ---- lifetime ---------------------------------------------------------- */

/* Like the library: cfmini works once per process and cfmfin is terminal. */
API void cfmini(int32_t *status) {
    CFM_ENTER(status);
    if (fin_count) { *status = HFIN; return; }
    if (initialized) { *status = S_ALREADY_INITIALIZED; return; }
    initialized = 1;
    ++init_count;
    *status = 0;
}

API void cfmfin(int32_t *status) {
    CFM_ENTER(status);
    if (fin_count) { *status = HFIN; return; }
    if (!initialized) { *status = S_NOT_INITIALIZED; return; }
    initialized = 0;
    ++fin_count;
    memset(handles, 0, sizeof handles);
    memset(cursors, 0, sizeof cursors);
    memset(work_objects, 0, sizeof work_objects);
    if (output) fclose(output);
    output = NULL;
    *status = 0;
}

API void cfmver(int32_t *status, float *version) {
    CFM_ENTER(status);
    if (!initialized) { *status = S_NOT_INITIALIZED; return; }
    *version = 4.25f;
    *status = 0;
}

/* Extended error mechanics. cfmlerr's signature here is the shim's own
 * candidate for testing the retrieval mechanics; it is not a vendor fact. */
API void cfmlerr(int32_t *status, int32_t *length) {
    CFM_ENTER(status);
    *length = (int32_t)strlen(error_text);
    *status = 0;
}

API void cfmferr(int32_t *status, char *buffer) {
    size_t cap;
    CFM_ENTER(status);
    cap = strlen(buffer); /* vendor help: truncates to the input buffer length */
    strncpy(buffer, error_text, cap);
    buffer[cap] = '\0';
    *status = 0;
}

/* ---- commands ---------------------------------------------------------- */

API void cfmfame(int32_t *status, const char *command) {
    size_t n;
    CFM_ENTER(status);
    if (!initialized) { *status = S_NOT_INITIALIZED; return; }
    n = strlen(command);
    if (n > 13 && strncmp(command, "output file(\"", 13) == 0 && strcmp(command + n - 3, "!\")") == 0) {
        char path[1024];
        size_t len = n - 16;
        if (len >= sizeof path) { *status = HBOPT; return; }
        memcpy(path, command + 13, len);
        path[len] = '\0';
        if (output) fclose(output);
        output = fopen(path, "ab");
        *status = output ? 0 : HBOPT;
        return;
    }
    if (strcmp(command, "output terminal") == 0) {
        if (output) fclose(output);
        output = NULL;
        *status = 0;
        return;
    }
    if (strncmp(command, "fail", 4) == 0) {
        int code = HFAMER;
        if (n > 5) sscanf(command + 5, "%d", &code);
        snprintf(error_text, sizeof error_text, "synthetic failure for %s", command);
        if (output) { fputs("partial output before failure\n", output); fflush(output); }
        *status = code;
        return;
    }
    if (strncmp(command, "display ", 8) == 0) {
        long left, right;
        if (sscanf(command + 8, "%ld+%ld", &left, &right) == 2) {
            /* Without a redirection the "terminal" is the process stdout. */
            fprintf(output ? output : stdout, "%ld\n", left + right);
            fflush(output ? output : stdout);
            *status = 0;
            return;
        }
    }
    if (output) { fprintf(output, "echo: %s\n", command); fflush(output); }
    *status = 0;
}

/* ---- options ----------------------------------------------------------- */

static int option_index(const char *name) {
    if (strcmp(name, "CLASS") == 0) return 0;
    if (strcmp(name, "TYPE") == 0) return 1;
    if (strcmp(name, "FREQUENCY") == 0) return 2;
    if (strcmp(name, "ALIAS") == 0) return 3;
    return -1;
}

API void cfmsopt(int32_t *status, const char *name, const char *value) {
    char option[64], label[32];
    int on, idx, fields;
    CFM_ENTER(status);
    if (strcmp(value, "ON") == 0) on = 1;
    else if (strcmp(value, "OFF") == 0) on = 0;
    else { *status = HBOPT; return; }
    label[0] = '\0';
    fields = sscanf(name, "ITEM %63s %31s", option, label);
    if (fields < 1 || (idx = option_index(option)) < 0) { *status = HBOPT; return; }
    if (fields == 1) {
        option_all[idx] = on;
        option_counts[idx] = 0;
    } else if (on && option_counts[idx] < 8) {
        strncpy(option_values[idx][option_counts[idx]], label, 31);
        option_values[idx][option_counts[idx]][31] = '\0';
        ++option_counts[idx];
    }
    *status = 0;
}

static int option_allows(int idx, const char *label) {
    int k;
    if (option_all[idx]) return 1;
    for (k = 0; k < option_counts[idx]; ++k)
        if (strcmp(option_values[idx][k], label) == 0) return 1;
    return 0;
}

static const char *type_label(int32_t type) {
    switch (type) {
    case 1: return "NUMERIC";
    case 2: return "NAMELIST";
    case 3: return "BOOLEAN";
    case 4: return "STRING";
    case 5: return "PRECISION";
    default: return type >= 8 ? "DATE" : "UNDEFINED";
    }
}

static const char *freq_label(int32_t freq) {
    switch (freq) {
    case 0: return "UNDEFINED";
    case 129: return "MONTHLY";
    case 162: return "QUARTERLY_DECEMBER";
    case 232: return "CASE";
    default: return "OTHER";
    }
}

/* ---- databases --------------------------------------------------------- */

API void cfmopwk(int32_t *status, int32_t *key) {
    int k;
    CFM_ENTER(status);
    if (!initialized) { *status = S_NOT_INITIALIZED; return; }
    for (k = 0; k < MAX_DB; ++k)
        if (handles[k].used && handles[k].is_work) { *status = S_EXISTS; return; }
    for (k = 0; k < MAX_DB; ++k) {
        if (!handles[k].used) {
            handles[k].used = 1;
            handles[k].mode = 4;
            handles[k].is_work = 1;
            strcpy(handles[k].name, "WORK");
            memcpy(handles[k].objects, work_objects, sizeof work_objects);
            *key = k;
            *status = 0;
            return;
        }
    }
    *status = S_BAD_KEY;
}

API void cfmopdb(int32_t *status, int32_t *key, const char *name, int32_t mode) {
    Store *store;
    int k;
    CFM_ENTER(status);
    if (!initialized) { *status = S_NOT_INITIALIZED; return; }
    if (mode < 1 || mode > 7) { *status = S_BAD_MODE; return; }
    if (strlen(name) >= 512) { *status = HBOPT; return; }
    store = find_store(name);
    if (mode == 2 && store) { *status = S_EXISTS; return; }
    if (mode != 2 && mode != 3 && !store) { *status = S_MISSING; return; }
    if (!store) {
        for (k = 0; k < MAX_STORE; ++k) if (!stores[k].used) { store = &stores[k]; break; }
        if (!store) { *status = S_BAD_KEY; return; }
        memset(store, 0, sizeof *store);
        store->used = 1;
        strcpy(store->name, name);
    } else if (mode == 3) {
        memset(store->objects, 0, sizeof store->objects);
    }
    for (k = 0; k < MAX_DB; ++k) {
        if (!handles[k].used) {
            handles[k].used = 1;
            handles[k].mode = mode;
            handles[k].is_work = 0;
            strcpy(handles[k].name, name);
            memcpy(handles[k].objects, store->objects, sizeof store->objects);
            *key = k;
            *status = 0;
            return;
        }
    }
    *status = S_BAD_KEY;
}

API void cfmpodb(int32_t *status, int32_t key) {
    Handle *h;
    Store *store;
    CFM_ENTER(status);
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (h->is_work) { memcpy(work_objects, h->objects, sizeof work_objects); *status = 0; return; }
    if (h->mode == 1) { *status = S_READONLY; return; }
    store = find_store(h->name);
    if (!store) { *status = S_MISSING; return; }
    memcpy(store->objects, h->objects, sizeof store->objects);
    *status = 0;
}

API void cfmcldb(int32_t *status, int32_t key) {
    Handle *h;
    CFM_ENTER(status);
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    memset(h, 0, sizeof *h);
    *status = 0;
}

/* ---- objects ----------------------------------------------------------- */

API void cfmnwob(int32_t *status, int32_t key, const char *name, int32_t cls, int32_t freq,
                 int32_t type, int32_t basis, int32_t observed) {
    Handle *h;
    int k;
    CFM_ENTER(status);
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (h->mode == 1) { *status = S_READONLY; return; }
    if (strlen(name) > 242) { *status = S_NAME_TOO_LONG; return; }
    if (cls != 1 && cls != 2) { *status = HBOPT; return; }
    if (find_object(h, name)) { *status = S_EXISTS; return; }
    for (k = 0; k < MAX_OBJ; ++k) {
        Object *o = &h->objects[k];
        if (!o->used) {
            memset(o, 0, sizeof *o);
            o->used = 1;
            upper_copy(o->name, name, sizeof o->name);
            o->cls = cls; o->freq = freq; o->type = type; o->basis = basis; o->observed = observed;
            o->first = cls == 1 ? FAME_INDEX_NC : 0;
            o->last = cls == 1 ? FAME_INDEX_NC : 0;
            *status = 0;
            return;
        }
    }
    *status = S_BAD_KEY;
}

API void cfmdlob(int32_t *status, int32_t key, const char *name) {
    Handle *h;
    Object *o;
    CFM_ENTER(status);
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (h->mode == 1) { *status = S_READONLY; return; }
    if (!(o = find_object(h, name))) { *status = HNOOBJ; return; }
    memset(o, 0, sizeof *o);
    *status = 0;
}

API int32_t fame_quick_info(int32_t key, const char *name, int32_t *cls, int32_t *type,
                            int32_t *freq, int64_t *first, int64_t *last) {
    Handle *h;
    Object *o;
    FAME_ENTER();
    if (!(h = handle_of(key))) return S_BAD_KEY;
    if (!(o = find_object(h, name))) return HNOOBJ;
    *cls = o->cls; *type = o->type; *freq = o->freq; *first = o->first; *last = o->last;
    return 0;
}

/* ---- data helpers ------------------------------------------------------ */

static int kind_of(const Object *o) {
    if (o->type >= 8) return 'i';
    switch (o->type) {
    case 5: return 'd';
    case 1: return 'f';
    case 3: return 'b';
    case 4: return 's';
    default: return 'n';
    }
}

static int locate(Handle **h, Object **o, int32_t key, const char *name, int kind,
                  const TestRange *range, int *offset, int *count) {
    if (!(*h = handle_of(key))) return S_BAD_KEY;
    if (!(*o = find_object(*h, name))) return HNOOBJ;
    if (kind_of(*o) != kind) return S_TYPE_MISMATCH;
    if (range == NULL) {
        if ((*o)->cls != 2) return S_RANGE;
        *offset = 0; *count = 1;
        return 0;
    }
    if ((*o)->cls != 1 || range->frequency != (*o)->freq) return S_RANGE;
    if (range->end < range->start || range->end - range->start + 1 > MAX_VALUES) return S_RANGE;
    *count = (int)(range->end - range->start + 1);
    if (!(*o)->has_data) { *offset = -1; return 0; }
    if (range->start < (*o)->first || range->end > (*o)->last) return S_RANGE;
    *offset = (int)(range->start - (*o)->first);
    return 0;
}

static int prepare_write(Object *o, const TestRange *range, int *offset, int count) {
    if (range == NULL) { o->has_data = 1; o->count = 1; *offset = 0; return 0; }
    if (!o->has_data) {
        o->has_data = 1; o->first = range->start; o->last = range->end; o->count = count;
        *offset = 0;
        return 0;
    }
    /* Overwrite inside the existing range only (extension is not modeled). */
    if (range->start < o->first || range->end > o->last) return S_RANGE;
    *offset = (int)(range->start - o->first);
    return 0;
}

#define GET_IMPL(FN, KIND, FIELD, T) \
API int32_t FN(int32_t key, const char *name, const TestRange *range, T *values) { \
    Handle *h; Object *o; int offset, count, st, k; \
    FAME_ENTER(); \
    if ((st = locate(&h, &o, key, name, KIND, range, &offset, &count))) return st; \
    if (offset < 0 || !o->has_data) return S_RANGE; \
    for (k = 0; k < count; ++k) values[k] = o->FIELD[offset + k]; \
    return 0; \
}

#define WRITE_IMPL(FN, KIND, FIELD, T) \
API int32_t FN(int32_t key, const char *name, const TestRange *range, const T *values) { \
    Handle *h; Object *o; int offset, count, st, k; \
    FAME_ENTER(); \
    if ((st = locate(&h, &o, key, name, KIND, range, &offset, &count))) return st; \
    if (h->mode == 1) return S_READONLY; \
    if ((st = prepare_write(o, range, &offset, count))) return st; \
    for (k = 0; k < count; ++k) o->FIELD[offset + k] = values[k]; \
    return 0; \
}

GET_IMPL(fame_get_precisions, 'd', d, double)
GET_IMPL(fame_get_numerics, 'f', f, float)
GET_IMPL(fame_get_booleans, 'b', b, int32_t)
GET_IMPL(fame_get_dates, 'i', i, int64_t)
WRITE_IMPL(fame_write_precisions, 'd', d, double)
WRITE_IMPL(fame_write_numerics, 'f', f, float)
WRITE_IMPL(fame_write_booleans, 'b', b, int32_t)

API int32_t fame_write_dates(int32_t key, const char *name, const TestRange *range, int32_t type,
                             const int64_t *values) {
    Handle *h; Object *o; int offset, count, st, k;
    FAME_ENTER();
    if ((st = locate(&h, &o, key, name, 'i', range, &offset, &count))) return st;
    if (h->mode == 1) return S_READONLY;
    if (o->type != type) return S_TYPE_MISMATCH;
    if ((st = prepare_write(o, range, &offset, count))) return st;
    for (k = 0; k < count; ++k) o->i[offset + k] = values[k];
    return 0;
}

API int32_t fame_len_strings(int32_t key, const char *name, const TestRange *range,
                             int32_t *lengths) {
    Handle *h; Object *o; int offset, count, st, k;
    FAME_ENTER();
    if ((st = locate(&h, &o, key, name, 's', range, &offset, &count))) return st;
    if (offset < 0 || !o->has_data) return S_RANGE;
    for (k = 0; k < count; ++k) lengths[k] = (int32_t)strlen(o->s[offset + k]);
    return 0;
}

API int32_t fame_get_strings(int32_t key, const char *name, const TestRange *range,
                             char **values, int32_t *lengths, int32_t *missing) {
    Handle *h; Object *o; int offset, count, st, k;
    FAME_ENTER();
    if ((st = locate(&h, &o, key, name, 's', range, &offset, &count))) return st;
    if (offset < 0 || !o->has_data) return S_RANGE;
    for (k = 0; k < count; ++k) {
        const char *src = o->s[offset + k];
        size_t full = strlen(src);
        size_t cap = lengths[k] < 0 ? 0 : (size_t)lengths[k];
        size_t used = full < cap ? full : cap;
        memcpy(values[k], src, used);
        values[k][used] = '\0';
        lengths[k] = (int32_t)full;
        if (missing) missing[k] = string_missing(src);
    }
    return 0;
}

API int32_t fame_write_strings(int32_t key, const char *name, const TestRange *range,
                               char **values) {
    Handle *h; Object *o; int offset, count, st, k;
    FAME_ENTER();
    if ((st = locate(&h, &o, key, name, 's', range, &offset, &count))) return st;
    if (h->mode == 1) return S_READONLY;
    for (k = 0; k < count; ++k) if (strlen(values[k]) >= MAX_STRING) return S_RANGE;
    if ((st = prepare_write(o, range, &offset, count))) return st;
    for (k = 0; k < count; ++k) strcpy(o->s[offset + k], values[k]);
    return 0;
}

/* ---- namelists --------------------------------------------------------- */

API void cfmnlen(int32_t *status, int32_t key, const char *name, int32_t which, int32_t *length) {
    Handle *h; Object *o;
    CFM_ENTER(status);
    if (which != -1) { *status = HBOPT; return; }
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (!(o = find_object(h, name))) { *status = HNOOBJ; return; }
    if (o->type != 2) { *status = S_TYPE_MISMATCH; return; }
    *length = (int32_t)strlen(o->namelist);
    *status = 0;
}

API void cfmgtnl(int32_t *status, int32_t key, const char *name, int32_t which, char *buffer,
                 int32_t capacity, int32_t *length) {
    Handle *h; Object *o; size_t full, cap, used;
    CFM_ENTER(status);
    if (which != -1) { *status = HBOPT; return; }
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (!(o = find_object(h, name))) { *status = HNOOBJ; return; }
    if (o->type != 2) { *status = S_TYPE_MISMATCH; return; }
    full = strlen(o->namelist);
    cap = capacity < 0 ? 0 : (size_t)capacity;
    used = full < cap ? full : cap;
    memcpy(buffer, o->namelist, used);
    buffer[used] = '\0';
    *length = (int32_t)full;
    *status = full > cap ? HTRUNC : 0;
}

API void cfmwtnl(int32_t *status, int32_t key, const char *name, int32_t which, const char *text) {
    Handle *h; Object *o;
    CFM_ENTER(status);
    if (which != -1) { *status = HBOPT; return; }
    if (!(h = handle_of(key))) { *status = S_BAD_KEY; return; }
    if (h->mode == 1) { *status = S_READONLY; return; }
    if (!(o = find_object(h, name))) { *status = HNOOBJ; return; }
    if (o->type != 2) { *status = S_TYPE_MISMATCH; return; }
    if (strlen(text) >= sizeof o->namelist) { *status = S_RANGE; return; }
    strcpy(o->namelist, text);
    o->has_data = 1;
    *status = 0;
}

/* ---- wildcards --------------------------------------------------------- */

static int match(const char *pattern, const char *text) {
    if (*pattern == '\0') return *text == '\0';
    if (*pattern == '?') {
        const char *t;
        for (t = text; ; ++t) {
            if (match(pattern + 1, t)) return 1;
            if (*t == '\0') return 0;
        }
    }
    if (*text == '\0') return 0;
    if (*pattern == '^' || toupper((unsigned char)*pattern) == *text)
        return match(pattern + 1, text + 1);
    return 0;
}

API int32_t fame_init_wildcard(int32_t key, int32_t *cursor_key, const char *pattern,
                               int32_t reserved, const char *unused) {
    Handle *h; int c, k;
    (void)reserved; (void)unused;
    FAME_ENTER();
    if (!(h = handle_of(key))) return S_BAD_KEY;
    if (pattern[0] == '\0') return S_BAD_WILDCARD;
    for (c = 0; c < MAX_CURSORS; ++c) {
        if (!cursors[c].used) {
            cursors[c].used = 1; cursors[c].position = 0; cursors[c].count = 0; cursors[c].db = key;
            for (k = 0; k < MAX_OBJ; ++k) {
                Object *o = &h->objects[k];
                /* ITEM FREQUENCY selections are recorded but not applied (observed). */
                if (o->used && match(pattern, o->name)
                    && option_allows(0, o->cls == 1 ? "SERIES" : "SCALAR")
                    && option_allows(1, type_label(o->type)))
                    cursors[c].object_index[cursors[c].count++] = k;
            }
            *cursor_key = c;
            return 0;
        }
    }
    return S_BAD_KEY;
}

API int32_t fame_get_next_wildcard(int32_t cursor_key, char *name, int32_t *cls, int32_t *type,
                                   int32_t *freq, int64_t *first, int64_t *last, int32_t inlen,
                                   int32_t *outlen) {
    Cursor *c; Handle *h; Object *o; size_t full, cap;
    FAME_ENTER();
    if (cursor_key < 0 || cursor_key >= MAX_CURSORS || !cursors[cursor_key].used) return S_BAD_KEY;
    c = &cursors[cursor_key];
    if (c->position >= c->count) return HNOOBJ;
    if (!(h = handle_of(c->db))) return S_BAD_KEY;
    o = &h->objects[c->object_index[c->position++]];
    full = strlen(o->name);
    cap = inlen < 0 ? 0 : (size_t)inlen;
    memcpy(name, o->name, full < cap ? full : cap);
    name[full < cap ? full : cap] = '\0';
    *outlen = (int32_t)full;
    *cls = o->cls; *type = o->type; *freq = o->freq;
    /* The reference notes that wildcard ranges are unreliable for scalars. */
    *first = o->cls == 1 ? o->first : -7;
    *last = o->cls == 1 ? o->last : -7;
    return full > cap ? HTRUNC : 0;
}

API int32_t fame_free_wildcard(int32_t cursor_key) {
    FAME_ENTER();
    if (cursor_key < 0 || cursor_key >= MAX_CURSORS || !cursors[cursor_key].used) return S_BAD_KEY;
    memset(&cursors[cursor_key], 0, sizeof cursors[cursor_key]);
    return 0;
}

/* ---- classification and calendar -------------------------------------- */

API void cfmispm(int32_t *status, double value, int32_t *type) {
    CFM_ENTER(status);
    *type = memcmp(&value, &FPRCNC, 8) == 0 ? 1 : memcmp(&value, &FPRCNA, 8) == 0 ? 2
            : memcmp(&value, &FPRCND, 8) == 0 ? 3 : 0;
    *status = 0;
}

API void cfmisnm(int32_t *status, float value, int32_t *type) {
    CFM_ENTER(status);
    *type = memcmp(&value, &FNUMNC, 4) == 0 ? 1 : memcmp(&value, &FNUMNA, 4) == 0 ? 2
            : memcmp(&value, &FNUMND, 4) == 0 ? 3 : 0;
    *status = 0;
}

API void cfmisbm(int32_t *status, int32_t value, int32_t *type) {
    CFM_ENTER(status);
    *type = value == FBOONC ? 1 : value == FBOONA ? 2 : value == FBOOND ? 3 : 0;
    *status = 0;
}

API void cfmissm(int32_t *status, const char *value, int32_t *type) {
    CFM_ENTER(status);
    *type = string_missing(value);
    *status = 0;
}

API int32_t fame_date_missing_type(int64_t value, int32_t *type) {
    FAME_ENTER();
    *type = value == FAME_INDEX_NC ? 1 : value == FAME_INDEX_NA ? 2 : value == FAME_INDEX_ND ? 3 : 0;
    return 0;
}

API int32_t fame_index_to_year_period(int32_t freq, int64_t index, int32_t *year, int32_t *period) {
    FAME_ENTER();
    if (freq == 0) return HBOPT;
    if (freq == 129) { *year = (int32_t)(index / 12); *period = (int32_t)(index % 12) + 1; return 0; }
    *year = (int32_t)(index / 1000); *period = (int32_t)(index % 1000);
    return 0;
}

API int32_t fame_year_period_to_index(int32_t freq, int64_t *index, int32_t year, int32_t period) {
    FAME_ENTER();
    if (freq == 0) return HBOPT;
    if (freq == 129) {
        if (period < 1 || period > 12) return HBOPT;
        *index = (int64_t)year * 12 + period - 1;
        return 0;
    }
    *index = (int64_t)year * 1000 + period;
    return 0;
}
