# Security

PerfLens puts an agent on a device and lets a server drive it. This document
says what that protects and — more importantly — what it does not.

## Reporting a vulnerability

Use [GitHub private security advisories][advisories] rather than a public
issue. Include the version (`perflens-agent --version`, `perflens version`),
how the agent was launched, and what an attacker would need to reach.

[advisories]: https://github.com/harshithsunku/perflens/security/advisories/new

---

## Threat model

**What is protected.** A peer must prove knowledge of a shared secret before
the agent will run any command. Without it the agent will not start profiling,
will not enumerate processes, will not stream device metrics, and will not
update itself.

**What is not protected: the channel.** The wire protocol is plaintext TCP.
There is no encryption, no integrity protection, and no defence against an
attacker who can read or modify traffic between the server and the agent. The
pairing code itself travels server → agent in the clear.

That is a deliberate trade for a tool aimed at controlled networks — lab
benches, test racks, a device on your desk. **If the network between you and
the device is not trusted, tunnel it:**

```bash
# On the device
perflens-agent --listen --bind 127.0.0.1

# On your machine
ssh -N -L 9999:127.0.0.1:9999 user@device
# then point the Live Debug wizard at 127.0.0.1:9999
```

**Profiling data is sensitive.** A profile carries function names, source file
paths, stack traces and — with `list_processes` — the full command line of
every process on the device. Command lines routinely contain secrets. Treat a
saved session the way you would treat a core dump.

---

## Authentication

The agent always holds a secret:

- `--token SECRET` / `PERFLENS_TOKEN`, if you supply one.
- Otherwise, in `--listen` mode, it **generates a pairing code at startup** —
  16 random bytes from `/dev/urandom`, printed to its log — and the operator
  copies it into the server. There is no unauthenticated state to fall into.

The code is per agent start. Restarting the agent rotates it.

### The exchange

```
agent → server  flag 3   {"type":"hello","version":1,"auth":"token",
                          "agent_version":"0.10.0","platform":{...}}

server → agent  flag 2   {"id":"<hex12>","cmd":"auth","args":{"token":"<code>"}}

agent  → server flag 3   {"id":"<hex12>","ok":true}
                    or   {"id":"<hex12>","ok":false,"error":"auth failed"}
```

The hello carries **no** secret. It is sent to whoever completed the TCP
handshake, before that peer has proved anything, so everything in it is public
by construction. Until the exchange succeeds, every command is answered
`{"ok":false,"error":"unauthenticated"}`, and no metrics are streamed.

Three wrong codes, or 30 seconds without a valid one, and the agent drops the
connection and returns to listening.

### Getting the code, in practice

The agent normally runs detached, so read it from wherever its output went:

```bash
ssh user@device 'grep -i "pairing code" /tmp/agent.log'
```

### Prefer the environment variable over the flag

`/proc/<pid>/cmdline` is world-readable, and PerfLens's own `list_processes`
reports it. A secret passed as `--token` is visible to every local user; one
passed via `PERFLENS_TOKEN` is not. A *generated* pairing code never appears
in argv at all, which is the safest of the three.

### `--bind`

`--listen` binds `0.0.0.0` by default, because reaching the device from
another machine is the point. Narrow it with `--bind 127.0.0.1` (plus an ssh
tunnel) when you want the port unreachable from the network.

---

## The web UI has no authentication

`perflens serve` binds `127.0.0.1` by default and the UI has no login. Anyone
who can reach the HTTP port can drive a connected agent and read every saved
session.

**Do not pass `--http-bind 0.0.0.0` on a shared or untrusted network.** Use an
ssh tunnel to the server instead:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@server
```

`--token` protects the *agent* connection. It does not protect the HTTP port.

---

## Software updates

`perflens-agent --update` downloads a binary from the project's GitHub
releases over HTTPS and replaces itself. Two things worth knowing:

- **The `--version` check is not integrity verification.** The agent runs the
  downloaded binary and looks for `perflens-agent` in its output. That catches
  a wrong-architecture or truncated download. It does not catch a hostile one
  — by then the binary has already executed.
- **Checksums are published, and verified by the installers, not the agent.**
  Each release asset ships a `.sha256` sidecar. `install-agent.sh` and
  `perflens push-agent` verify it; the C agent does not, because it carries no
  hash implementation and will not grow one just for this. A sidecar served
  from the same origin as the asset stops truncation, corruption and a
  poisoned single object. It does not stop an attacker who controls the
  origin.

`update` is refused on a session with no configured pairing code. To update a
tokenless agent, use ssh:

```bash
ssh user@device '~/.perflens/bin/perflens-agent --update'
```

Setting `PERFLENS_UPDATE_URL` to an `http://` origin is refused outright.

---

## Compatibility with pre-0.10.0 agents

Agents before 0.10.0 put their shared secret **in the hello frame** — which,
in `--listen` mode, meant handing it to anyone who completed a TCP handshake.
A 0.10.0 server still accepts such an agent when the token matches, logs a
warning, and strips the token before the hello reaches the HTTP API. Upgrade
those agents.

**Upgrade order: server first, then agents.** A 0.10.0 agent sends no hello
token, so an older server configured with `--token` will reject it.

## Deprecation timeline

| Version | Change |
|---------|--------|
| 0.10.0  | Pairing-code authentication. Legacy hello tokens accepted with a warning. |
| 0.11.0  | Legacy hello tokens refused when the server has a token configured. |
| 1.0.0   | A pairing code is required for both `--listen` and `--server`; only headless `--output` runs without one. |

## Supported versions

Fixes land on the latest release. There are no backports to earlier lines.
