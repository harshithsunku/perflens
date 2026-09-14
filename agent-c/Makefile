# PerfLens C Agent — Makefile
#
# Usage:
#   make                               # native build
#   make CROSS=x86_64-linux-musl-      # x86_64 (musl, fully static)
#   make CROSS=aarch64-linux-musl-     # ARM64 little-endian (musl)
#   make CROSS=aarch64_be-linux-musl-  # ARM64 big-endian (musl)
#   make CROSS=arm-linux-musleabi-     # ARMv7 32-bit little-endian (musl, soft-float)
#   make CROSS=armeb-linux-musleabi-   # ARMv7 32-bit big-endian (musl, soft-float)
#   make all-cross                     # all cross targets
#   make check                         # native unit tests (metrics parsers)
#   make SANITIZE=address,undefined    # instrumented native build
#
# Every release asset is a musl static build: static glibc cannot reliably
# resolve names (getaddrinfo needs the host's NSS libraries at run time), and
# the toolchains all come from the repo's `toolchains` GitHub release.
#   make clean

CC      = $(CROSS)gcc
VERSION := $(shell cat ../VERSION 2>/dev/null || echo dev)
# Hardening for a network-facing daemon: stack protector, fortified libc
# calls (glibc; musl ignores the macro), format-string warnings as a gate,
# and a read-only GOT/PLT after immediate binding. Per-function sections plus
# --gc-sections drop the unused half of the vendored zstd (the agent only
# compresses): measured -19% on x86_64, -30% on armeb.
HARDEN  = -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
          -Wformat=2 -Wformat-security \
          -ffunction-sections -fdata-sections
CFLAGS  = -std=c11 -O2 -Wall -Wextra -Wno-unused-parameter \
          -D_POSIX_C_SOURCE=200809L -D_GNU_SOURCE \
          -DAGENT_VERSION=\"$(VERSION)\" -I. $(HARDEN) $(ARCHFLAGS)
LDFLAGS = -static -Wl,-z,relro,-z,now -Wl,--gc-sections -lpthread -lm

# `make SANITIZE=address,undefined` (or `thread`) builds an instrumented,
# dynamically linked native agent for the protocol tests; CI runs both.
ifneq ($(strip $(SANITIZE)),)
  CFLAGS  += -fsanitize=$(SANITIZE) -fno-omit-frame-pointer -g -O1
  LDFLAGS  = -fsanitize=$(SANITIZE) -lpthread -lm
endif

# Big-endian ARM needs an explicit -march, and the soft-float toolchain.
#
# Two separate traps, both measured on a big-endian ARMv7 target (armv7b,
# kernel 4.4) rather than reasoned about:
#
#   * The armeb toolchain defaults to ARMv5, which emits BE-32. ARMv6 and
#     later implement BE-8 only, so a default-arch build dies on its first
#     instruction. Without -march the hello-world binary is SIGILL.
#   * That CPU reports no `vfp` and its own binaries are soft-float
#     (e_flags & 0x200). A hard-float build is SIGILL there too, which is
#     what `armeb-linux-musleabihf-` produced. Soft-float is also the
#     strictly more portable choice for a single published asset: it runs on
#     VFP hardware as well, where hard-float does not run without one.
#
# Verify with: readelf -h <bin> | grep Flags   ->  soft-float ABI, BE8
ifneq (,$(findstring armeb,$(CROSS)))
  ARCHFLAGS = -march=armv7-a
endif
# The little-endian 32-bit asset is soft-float too, for the same reason: it
# runs on VFP hardware as well, where a hard-float build does not run
# without one. The musl toolchain also defaults to ARMv5; ask for v7.
ifneq (,$(findstring arm-linux-musleabi-,$(CROSS)))
  ARCHFLAGS = -march=armv7-a
endif

TARGET  = perflens-agent
SRCS    = $(wildcard src/*.c)

# A native build lands at ./perflens-agent — the path the test suite looks in.
# A cross build lands in build/<arch>/ instead, and gets its own object dir, so
# cross-compiling can never leave a foreign binary (or foreign .o files) where
# the native one is expected. Ask for the path with `make print-bin CROSS=...`
# rather than assuming it.
ifeq ($(strip $(CROSS)),)
  OUTDIR = .
else
  OUTDIR = build/$(firstword $(subst -, ,$(CROSS)))
endif

OBJDIR = $(OUTDIR)/obj
BIN    = $(OUTDIR)/$(TARGET)
OBJS   = $(patsubst src/%.c,$(OBJDIR)/%.o,$(SRCS)) $(OBJDIR)/zstd.o

.PHONY: all clean all-cross print-bin check

all: $(BIN)

# Native unit tests for the parsers that read /proc and /sys. They include
# the module source directly, so the static parsers are reachable.
check: $(OBJDIR)/test_metrics
	$(OBJDIR)/test_metrics

$(OBJDIR)/test_metrics: tests/test_metrics.c src/metrics.c src/agent.h $(OBJDIR)/util.o $(OBJDIR)/subproc.o $(OBJDIR)/collect.o $(OBJDIR)/wire.o $(OBJDIR)/perfcmd.o $(OBJDIR)/probe.o $(OBJDIR)/zstd.o $(OBJDIR)/commands.o $(OBJDIR)/procs.o $(OBJDIR)/auth.o $(OBJDIR)/update.o
	@mkdir -p $(OBJDIR)
	$(CC) $(CFLAGS) -o $@ tests/test_metrics.c $(filter %.o,$^) $(LDFLAGS)

print-bin:
	@echo $(BIN)

$(BIN): $(OBJS)
	@mkdir -p $(OUTDIR)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(LDFLAGS)
	@if [ -z "$(SANITIZE)" ] && command -v file >/dev/null 2>&1 && \
	    ! file $@ | grep -Eq 'static(ally|-pie) linked'; then \
	    echo "error: $@ is not statically linked:"; file $@; exit 1; fi
	@echo "Built: $@"

$(OBJDIR)/%.o: src/%.c src/agent.h vendor/zstd/zstd.h
	@mkdir -p $(OBJDIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR)/zstd.o: vendor/zstd/zstd.c vendor/zstd/zstd.h
	@mkdir -p $(OBJDIR)
	$(CC) $(CFLAGS) -DZSTD_DISABLE_ASM -c -o $@ $<

clean:
	rm -rf $(TARGET) obj/ build/
	@# Object files from builds that predate the per-arch layout.
	rm -f src/*.o vendor/zstd/*.o

# Cross-compile all supported targets — each into its own build/<arch>/.
# No `clean` between arches: separate object dirs make the builds independent.
CROSS_TARGETS = x86_64-linux-musl- aarch64-linux-musl- aarch64_be-linux-musl- arm-linux-musleabi- armeb-linux-musleabi-

all-cross:
	@for cross in $(CROSS_TARGETS); do \
		arch=$$(echo $$cross | cut -d- -f1); \
		echo "=== Building for $$arch (CROSS=$$cross) ==="; \
		$(MAKE) CROSS=$$cross || echo "  SKIP: $$cross toolchain not found"; \
	done
	@echo "=== Cross builds complete ==="
	@ls -la build/*/$(TARGET) 2>/dev/null || true
