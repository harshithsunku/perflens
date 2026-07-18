/*
 * PerfLens Device Agent — logging, buffers, string/JSON helpers
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Logging
 * -------------------------------------------------------------------------- */

void agent_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s ", LOG_PREFIX);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    fflush(stderr);
    va_end(ap);
}

void agent_warn(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s WARNING: ", LOG_PREFIX);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    fflush(stderr);
    va_end(ap);
}

/* --------------------------------------------------------------------------
 * Dynamic buffer
 * -------------------------------------------------------------------------- */

void buf_init(struct buf *b)
{
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

void buf_free(struct buf *b)
{
    free(b->data);
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

int buf_ensure(struct buf *b, size_t needed)
{
    if (b->cap >= needed) return 0;
    if (needed > MAX_BUF_SIZE) return -1;
    size_t newcap = b->cap ? b->cap : INITIAL_BUF_SIZE;
    while (newcap < needed) newcap *= 2;
    if (newcap > MAX_BUF_SIZE) newcap = MAX_BUF_SIZE;
    char *p = realloc(b->data, newcap);
    if (!p) return -1;
    b->data = p;
    b->cap  = newcap;
    return 0;
}

/* --------------------------------------------------------------------------
 * String helpers
 * -------------------------------------------------------------------------- */

int str_contains_lower(const char *haystack, size_t len, const char *needle)
{
    size_t nlen = strlen(needle);
    if (nlen > len) return 0;
    for (size_t i = 0; i <= len - nlen; i++) {
        size_t j;
        for (j = 0; j < nlen; j++) {
            char c = haystack[i + j];
            if (c >= 'A' && c <= 'Z') c += 32;
            if (c != needle[j]) break;
        }
        if (j == nlen) return 1;
    }
    return 0;
}

/* Events that can only be used with perf stat, not perf record */
static const char *STAT_ONLY_EVENTS[] = {
    "page-faults", "context-switches", "cpu-migrations",
    NULL
};

int is_stat_only(const char *event)
{
    for (int i = 0; STAT_ONLY_EVENTS[i]; i++)
        if (strcmp(event, STAT_ONLY_EVENTS[i]) == 0) return 1;
    return 0;
}

/* --------------------------------------------------------------------------
 * Minimal JSON helpers
 *
 * Sufficient for the well-defined PerfLens wire protocol. Not a general
 * JSON parser — only handles the command/response structures used here.
 * -------------------------------------------------------------------------- */

/* Escape a string for JSON output. Returns bytes written (excluding NUL). */
size_t json_escape(char *dst, size_t cap, const char *src)
{
    size_t pos = 0;
    for (; *src && pos + 2 < cap; src++) {
        switch (*src) {
        case '"':  dst[pos++] = '\\'; dst[pos++] = '"';  break;
        case '\\': dst[pos++] = '\\'; dst[pos++] = '\\'; break;
        case '\n': dst[pos++] = '\\'; dst[pos++] = 'n';  break;
        case '\r': dst[pos++] = '\\'; dst[pos++] = 'r';  break;
        case '\t': dst[pos++] = '\\'; dst[pos++] = 't';  break;
        default:
            if ((unsigned char)*src >= 0x20)
                dst[pos++] = *src;
            break;
        }
    }
    dst[pos] = '\0';
    return pos;
}

/* Find a JSON string value by key. Returns 0 on success, -1 if not found. */
int json_get_str(const char *json, const char *key, char *buf, size_t buflen)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return -1;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '"') return -1;
    p++;

    size_t i = 0;
    while (*p && *p != '"' && i + 1 < buflen) {
        if (*p == '\\' && *(p + 1)) {
            p++;
            switch (*p) {
            case '"':  buf[i++] = '"';  break;
            case '\\': buf[i++] = '\\'; break;
            case 'n':  buf[i++] = '\n'; break;
            case 'r':  buf[i++] = '\r'; break;
            case 't':  buf[i++] = '\t'; break;
            default:   buf[i++] = *p;   break;
            }
        } else {
            buf[i++] = *p;
        }
        p++;
    }
    buf[i] = '\0';
    return 0;
}

/* Find a JSON integer value by key. Returns 0 on success, -1 if not found. */
int json_get_int(const char *json, const char *key, int *out)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return -1;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;

    char *end;
    long val = strtol(p, &end, 10);
    if (end == p) return -1;
    *out = (int)val;
    return 0;
}

/* Find a JSON boolean by key. Sets *out to 0 or 1. Returns 0 on success. */
int json_get_bool(const char *json, const char *key, int *out)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return -1;
    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (strncmp(p, "true", 4) == 0) { *out = 1; return 0; }
    if (strncmp(p, "false", 5) == 0) { *out = 0; return 0; }
    return -1;
}

/* Find a nested JSON object by key. Returns pointer to '{' or NULL. */
const char *json_find_object(const char *json, const char *key)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '{') return NULL;
    return p;
}

/* Find a nested JSON array by key. Returns pointer to '[' or NULL. */
const char *json_find_array(const char *json, const char *key)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '[') return NULL;
    return p;
}

/* --------------------------------------------------------------------------
 * Process liveness check
 * -------------------------------------------------------------------------- */

int process_exists(int pid)
{
    if (kill(pid, 0) == 0) return 1;
    if (errno == EPERM)    return 1;  /* exists but we lack permission */
    return 0;
}

/* Read a single long integer from a /proc or /sys file. Returns -1 on error. */
long read_int_file(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    long val = -1;
    if (fscanf(f, "%ld", &val) != 1) val = -1;
    fclose(f);
    return val;
}

