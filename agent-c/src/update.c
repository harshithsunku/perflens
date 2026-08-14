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

/* Which release asset replaces *this* binary.
 *
 * Resolved at compile time, not from uname(). A 32-bit agent running on a
 * 64-bit kernel is a normal arrangement, and there uname() reports the
 * kernel's aarch64 — so asking it would quietly pull the 64-bit asset over
 * a 32-bit install. The binary already knows what it was built as.
 */
#if defined(__x86_64__)
#  define ASSET_ARCH "x86_64"
#elif defined(__aarch64__)
#  if defined(__AARCH64EB__)
#    define ASSET_ARCH "aarch64_be"
#  else
#    define ASSET_ARCH "aarch64"
#  endif
#elif defined(__arm__)
#  if defined(__ARMEB__)
#    define ASSET_ARCH "armeb"
#  else
#    define ASSET_ARCH "armv7"
#  endif
#endif

static int detect_asset_arch(char *buf, size_t buflen)
{
#ifdef ASSET_ARCH
    snprintf(buf, buflen, "%s", ASSET_ARCH);
    return 0;
#else
    (void)buf; (void)buflen;
    return -1;
#endif
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


