/*
 * PerfLens Device Agent — logging, buffers, string/JSON helpers
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Logging
 *
 * One formatted line per write(2). stdio locks per call, so the three
 * fprintf()s a line used to take let the collection, metrics and command
 * threads interleave mid-line; and a timestamp is what lets a field log be
 * matched against the server's.
 * -------------------------------------------------------------------------- */

static int g_log_debug = -1;

static int log_debug_enabled(void)
{
    if (g_log_debug < 0) {
        const char *lvl = getenv("PERFLENS_LOG");
        g_log_debug = (lvl && strcmp(lvl, "debug") == 0) ? 1 : 0;
    }
    return g_log_debug;
}

static void log_emit(const char *level, const char *fmt, va_list ap)
{
    char buf[2048];
    struct timespec ts;
    struct tm tm;
    clock_gettime(CLOCK_REALTIME, &ts);
    localtime_r(&ts.tv_sec, &tm);

    int n = snprintf(buf, sizeof(buf) - 1,
                     "%04d-%02d-%02dT%02d:%02d:%02d.%03ld %s %s",
                     tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                     tm.tm_hour, tm.tm_min, tm.tm_sec,
                     ts.tv_nsec / 1000000L, LOG_PREFIX, level);
    if (n < 0) return;
    if ((size_t)n < sizeof(buf) - 1) {
        int m = vsnprintf(buf + n, sizeof(buf) - 1 - (size_t)n, fmt, ap);
        if (m > 0) n += m;
    }
    if ((size_t)n > sizeof(buf) - 2) n = (int)sizeof(buf) - 2;
    buf[n++] = '\n';

    const char *p = buf;
    size_t left = (size_t)n;
    while (left > 0) {
        ssize_t w = write(STDERR_FILENO, p, left);
        if (w < 0) {
            if (errno == EINTR) continue;
            return;
        }
        p += w;
        left -= (size_t)w;
    }
}

void agent_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    log_emit("", fmt, ap);
    va_end(ap);
}

void agent_warn(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    log_emit("WARNING: ", fmt, ap);
    va_end(ap);
}

void agent_debug(const char *fmt, ...)
{
    if (!log_debug_enabled()) return;
    va_list ap;
    va_start(ap, fmt);
    log_emit("", fmt, ap);
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

int buf_ensure_small(struct buf *b, size_t needed, size_t initial)
{
    if (b->cap >= needed) return 0;
    if (needed > MAX_BUF_SIZE) return -1;
    size_t newcap = b->cap ? b->cap : initial;
    while (newcap < needed) newcap *= 2;
    if (newcap > MAX_BUF_SIZE) newcap = MAX_BUF_SIZE;
    char *p = realloc(b->data, newcap);
    if (!p) return -1;
    b->data = p;
    b->cap  = newcap;
    return 0;
}

int buf_ensure(struct buf *b, size_t needed)
{
    return buf_ensure_small(b, needed, INITIAL_BUF_SIZE);
}

/* --------------------------------------------------------------------------
 * String helpers
 * -------------------------------------------------------------------------- */

int str_contains_lower(const char *haystack, size_t len, const char *needle)
{
    if (!haystack) return 0;
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
    if (cap == 0) return 0;
    for (; src && *src && pos + 2 < cap; src++) {
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

/* Skip a JSON string starting at the opening quote. Returns a pointer past
 * the closing quote, or NULL. */
static const char *skip_string(const char *p, const char *end)
{
    p++;
    while (p < end && *p) {
        if (*p == '\\') { p += 2; continue; }
        if (*p == '"') return p + 1;
        p++;
    }
    return NULL;
}

const char *json_object_end(const char *obj)
{
    if (!obj || (*obj != '{' && *obj != '[')) return NULL;
    const char *end = obj + strlen(obj);
    int depth = 0;
    const char *p = obj;
    while (p < end && *p) {
        if (*p == '"') {
            p = skip_string(p, end);
            if (!p) return NULL;
            continue;
        }
        if (*p == '{' || *p == '[') depth++;
        else if (*p == '}' || *p == ']') {
            depth--;
            if (depth == 0) return p + 1;
        }
        p++;
    }
    return NULL;
}

/* Find `"key"` used as a key (a quoted name followed by ':') among the
 * direct members of the container at `json`, scanning [json, end). Nested
 * objects and arrays are skipped whole, so `{"args":{"pid":1},"pid":2}`
 * answers 2 for "pid" and an `args` object that happens to contain a "cmd"
 * key cannot shadow the command. Strings are skipped as units, so a value
 * that equals the key name cannot match either. Returns a pointer to the
 * value, or NULL. */
static const char *find_key(const char *json, const char *end, const char *key)
{
    if (!json) return NULL;
    if (!end) end = json + strlen(json);
    size_t klen = strlen(key);
    const char *p = json;
    int depth = 0;
    while (p < end && *p) {
        char c = *p;
        if (c == '{' || c == '[') { depth++; p++; continue; }
        if (c == '}' || c == ']') { depth--; p++; continue; }
        if (c != '"') { p++; continue; }
        const char *close = skip_string(p, end);
        if (!close) return NULL;
        if (depth != 1) { p = close; continue; }
        size_t slen = (size_t)(close - p) - 2;
        const char *after = close;
        while (after < end && (*after == ' ' || *after == '\t' ||
                               *after == '\n' || *after == '\r'))
            after++;
        if (slen == klen && memcmp(p + 1, key, klen) == 0 &&
            after < end && *after == ':') {
            after++;
            while (after < end && (*after == ' ' || *after == '\t' ||
                                   *after == '\n' || *after == '\r'))
                after++;
            return after;
        }
        p = close;
    }
    return NULL;
}

int json_get_str_n(const char *json, const char *end, const char *key,
                   char *buf, size_t buflen)
{
    const char *p = find_key(json, end, key);
    if (!p || *p != '"') return -1;
    p++;

    size_t i = 0;
    while (*p && *p != '"' && i + 1 < buflen) {
        if (*p == '\\' && *(p + 1)) {
            p++;
            switch (*p) {
            case '"':  buf[i++] = '"';  break;
            case '\\': buf[i++] = '\\'; break;
            case '/':  buf[i++] = '/';  break;
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

int json_get_int_n(const char *json, const char *end, const char *key,
                   int *out)
{
    const char *p = find_key(json, end, key);
    if (!p) return -1;
    char *e;
    errno = 0;
    long val = strtol(p, &e, 10);
    if (e == p) return -1;
    if (errno == ERANGE || val > INT_MAX || val < INT_MIN) return -1;
    *out = (int)val;
    return 0;
}

int json_get_bool_n(const char *json, const char *end, const char *key,
                    int *out)
{
    const char *p = find_key(json, end, key);
    if (!p) return -1;
    if (strncmp(p, "true", 4) == 0) { *out = 1; return 0; }
    if (strncmp(p, "false", 5) == 0) { *out = 0; return 0; }
    return -1;
}

const char *json_find_object_n(const char *json, const char *end,
                               const char *key)
{
    const char *p = find_key(json, end, key);
    if (!p || *p != '{') return NULL;
    return p;
}

const char *json_find_array_n(const char *json, const char *end,
                              const char *key)
{
    const char *p = find_key(json, end, key);
    if (!p || *p != '[') return NULL;
    return p;
}

int json_get_str(const char *json, const char *key, char *buf, size_t buflen)
{
    return json_get_str_n(json, NULL, key, buf, buflen);
}

int json_get_int(const char *json, const char *key, int *out)
{
    return json_get_int_n(json, NULL, key, out);
}

int json_get_bool(const char *json, const char *key, int *out)
{
    return json_get_bool_n(json, NULL, key, out);
}

const char *json_find_object(const char *json, const char *key)
{
    return json_find_object_n(json, NULL, key);
}

const char *json_find_array(const char *json, const char *key)
{
    return json_find_array_n(json, NULL, key);
}

int json_valid_id(const char *id)
{
    if (!id || !*id) return 0;
    size_t n = 0;
    for (; *id; id++, n++) {
        char c = *id;
        int ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                 (c >= '0' && c <= '9') || c == '_' || c == '.' ||
                 c == ':' || c == '-';
        if (!ok || n >= 63) return 0;
    }
    return 1;
}

/* --------------------------------------------------------------------------
 * Bounded string builder
 * -------------------------------------------------------------------------- */

void wbuf_init(struct wbuf *w, char *storage, size_t cap)
{
    w->p = storage;
    w->cap = cap;
    w->len = 0;
    w->truncated = 0;
    if (cap) storage[0] = '\0';
}

void wbuf_add(struct wbuf *w, const char *s)
{
    size_t n = strlen(s);
    if (w->truncated || w->len + n + 1 > w->cap) {
        w->truncated = 1;
        return;
    }
    memcpy(w->p + w->len, s, n + 1);
    w->len += n;
}

void wbuf_addf(struct wbuf *w, const char *fmt, ...)
{
    if (w->truncated) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(w->p + w->len, w->cap - w->len, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= w->cap - w->len) {
        w->truncated = 1;
        w->p[w->len] = '\0';
        return;
    }
    w->len += (size_t)n;
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

unsigned long long process_start_time(int pid)
{
    char path[64], line[1024];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char *got = fgets(line, sizeof(line), f);
    fclose(f);
    if (!got) return 0;

    /* comm may contain spaces or parens: fields start after the last ')' */
    char *p = strrchr(line, ')');
    if (!p) return 0;
    p++;
    int field = 3;
    while (*p) {
        while (*p == ' ') p++;
        if (!*p) break;
        if (field == 22) return strtoull(p, NULL, 10);
        while (*p && *p != ' ') p++;
        field++;
    }
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

/* --------------------------------------------------------------------------
 * Temp files
 * -------------------------------------------------------------------------- */

const char *agent_tmpdir(void)
{
    const char *d = getenv("TMPDIR");
    if (d && d[0] == '/') return d;
    return "/tmp";
}

void sweep_stale_tmpfiles(void)
{
    const char *dir = agent_tmpdir();
    DIR *d = opendir(dir);
    if (!d) return;
    time_t now = time(NULL);
    uid_t me = getuid();
    struct dirent *ent;
    int removed = 0;
    while ((ent = readdir(d)) != NULL) {
        if (strncmp(ent->d_name, "perflens-", 9) != 0) continue;
        char path[PATH_MAX];
        snprintf(path, sizeof(path), "%s/%s", dir, ent->d_name);
        struct stat st;
        if (lstat(path, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        if (st.st_uid != me) continue;
        if (now - st.st_mtime < 3600) continue;   /* another agent's live round */
        if (unlink(path) == 0) removed++;
    }
    closedir(d);
    if (removed)
        agent_log("Removed %d stale temp file%s from %s", removed,
                  removed == 1 ? "" : "s", dir);
}
