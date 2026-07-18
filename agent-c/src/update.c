/*
 * PerfLens Device Agent — self-update
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Self-update
 *
 * Downloads the release asset matching this machine's arch, verifies the
 * new binary runs, then atomically renames it over the running binary.
 * Everything stays user-space (no sudo); the running process keeps its old
 * inode until restarted.
 * -------------------------------------------------------------------------- */

static int detect_asset_arch(char *buf, size_t buflen)
{
    struct utsname u;
    if (uname(&u) != 0)
        return -1;
    const char *m = u.machine;

    union { uint16_t v; uint8_t b[2]; } probe;
    probe.v = 1;
    int little = (probe.b[0] == 1);

    if (strcmp(m, "x86_64") == 0)
        snprintf(buf, buflen, "x86_64");
    else if (strncmp(m, "aarch64", 7) == 0)
        snprintf(buf, buflen, "%s", little ? "aarch64" : "aarch64_be");
    else if (strncmp(m, "armeb", 5) == 0 ||
             (strncmp(m, "arm", 3) == 0 && !little))
        snprintf(buf, buflen, "armeb");
    else if (strncmp(m, "arm", 3) == 0)
        snprintf(buf, buflen, "armv7");
    else
        return -1;
    return 0;
}

/* Download url to dest via curl (preferred) or wget. exec failure = 127. */
static int download_file(const char *url, const char *dest)
{
    struct buf err;
    buf_init(&err);

    char *curl_argv[] = { (char *)"curl", (char *)"-fsSL",
                          (char *)"--connect-timeout", (char *)"20",
                          (char *)"-o", (char *)dest, (char *)url, NULL };
    int rc = run_cmd(curl_argv, NULL, &err, 300);
    if (rc == 127) {
        char *wget_argv[] = { (char *)"wget", (char *)"-q",
                              (char *)"-T", (char *)"20",
                              (char *)"-O", (char *)dest, (char *)url, NULL };
        rc = run_cmd(wget_argv, NULL, &err, 300);
        if (rc == 127) {
            agent_warn("Neither curl nor wget found — cannot download");
            buf_free(&err);
            unlink(dest);
            return -1;
        }
    }
    if (rc != 0) {
        agent_warn("Download failed (rc=%d): %.*s", rc,
                   (int)(err.len < 200 ? err.len : 200),
                   err.data ? err.data : "");
        buf_free(&err);
        unlink(dest);
        return -1;
    }
    buf_free(&err);
    return 0;
}

int self_update(char *msg, size_t msglen)
{
    char self[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (n <= 0) {
        snprintf(msg, msglen, "cannot resolve own binary path");
        return -1;
    }
    self[n] = '\0';

    char arch[32];
    if (detect_asset_arch(arch, sizeof(arch)) != 0) {
        snprintf(msg, msglen, "unsupported architecture");
        return -1;
    }

    const char *base = getenv("PERFLENS_UPDATE_URL");
    if (!base || !base[0])
        base = UPDATE_URL_BASE;

    char url[512];
    snprintf(url, sizeof(url), "%s/perflens-agent-linux-%s", base, arch);

    char tmp[PATH_MAX + 32];
    snprintf(tmp, sizeof(tmp), "%s.update.%d", self, (int)getpid());

    agent_log("Downloading %s ...", url);
    if (download_file(url, tmp) != 0) {
        snprintf(msg, msglen, "download failed: %.400s", url);
        return -1;
    }

    if (chmod(tmp, 0755) != 0) {
        snprintf(msg, msglen, "chmod failed: %s", strerror(errno));
        unlink(tmp);
        return -1;
    }

    /* Verify the downloaded binary actually runs before replacing self */
    struct buf out;
    buf_init(&out);
    char *ver_argv[] = { tmp, (char *)"--version", NULL };
    int rc = run_cmd(ver_argv, &out, NULL, 30);
    if (rc != 0 || out.len == 0 ||
        !str_contains_lower(out.data, out.len, "perflens-agent")) {
        snprintf(msg, msglen, "downloaded binary failed verification (rc=%d)", rc);
        buf_free(&out);
        unlink(tmp);
        return -1;
    }

    /* Extract "perflens-agent <version>" from the new binary's output */
    char new_version[64] = "unknown";
    if (out.data) {
        out.data[out.len < out.cap ? out.len : out.cap - 1] = '\0';
        const char *sp = strchr(out.data, ' ');
        if (sp) {
            snprintf(new_version, sizeof(new_version), "%s", sp + 1);
            char *nl = strpbrk(new_version, "\r\n");
            if (nl) *nl = '\0';
        }
    }
    buf_free(&out);

    if (strcmp(new_version, AGENT_VERSION) == 0) {
        snprintf(msg, msglen, "already up to date (%s)", AGENT_VERSION);
        unlink(tmp);
        return 0;
    }

    if (rename(tmp, self) != 0) {
        snprintf(msg, msglen, "rename failed: %s", strerror(errno));
        unlink(tmp);
        return -1;
    }

    snprintf(msg, msglen,
             "updated %s -> %s (restart the agent to run the new version)",
             AGENT_VERSION, new_version);
    return 0;
}


