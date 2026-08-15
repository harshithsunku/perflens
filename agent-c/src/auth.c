/* auth.c — pairing-code generation and constant-time comparison.
 *
 * The agent authenticates its peer with a shared secret it never transmits:
 * the server must present the code before any command is dispatched. The code
 * is either supplied by the operator (--token / PERFLENS_TOKEN) or generated
 * here at startup and printed to the log for the operator to copy across.
 *
 * There is deliberately no cryptography in this file. The exchange is a
 * comparison of a 128-bit random value, not a challenge/response, which is
 * what lets the agent stay a zero-dependency static binary with no vendored
 * hash implementation. The trade-off is that the code crosses the wire in
 * cleartext from server to agent, so the transport is only as private as the
 * network it runs on — see SECURITY.md.
 */

#include "agent.h"

#include <fcntl.h>   /* the only header agent.h does not already pull in */

int agent_generate_token(char *out, size_t out_cap)
{
    static const char HEX[] = "0123456789abcdef";
    unsigned char raw[TOKEN_BYTES];

    if (!out || out_cap <= (size_t)TOKEN_HEX_LEN)
        return -1;

    /* /dev/urandom rather than getrandom(2): the five cross targets include
     * musl and older glibc variants where the syscall wrapper is not
     * available, and this runs once at startup so the open() cost is
     * irrelevant. */
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0)
        return -1;

    size_t got = 0;
    while (got < sizeof(raw)) {
        ssize_t n = read(fd, raw + got, sizeof(raw) - got);
        if (n <= 0) {
            /* EINTR is worth retrying; anything else means we cannot get
             * trustworthy randomness, and a predictable pairing code is worse
             * than refusing to run. Callers must fail closed. */
            if (n < 0 && errno == EINTR)
                continue;
            close(fd);
            return -1;
        }
        got += (size_t)n;
    }
    close(fd);

    for (size_t i = 0; i < sizeof(raw); i++) {
        out[i * 2]     = HEX[(raw[i] >> 4) & 0x0f];
        out[i * 2 + 1] = HEX[raw[i] & 0x0f];
    }
    out[TOKEN_HEX_LEN] = '\0';
    return 0;
}

int agent_consttime_eq(const char *a, const char *b)
{
    if (!a || !b)
        return 0;

    size_t la = strlen(a);
    size_t lb = strlen(b);
    if (la != lb)
        return 0;

    /* Accumulate differences across the whole string instead of returning at
     * the first mismatch, so the time taken does not reveal how much of the
     * candidate was correct. */
    unsigned char diff = 0;
    for (size_t i = 0; i < la; i++)
        diff |= (unsigned char)(a[i] ^ b[i]);

    return diff == 0;
}
