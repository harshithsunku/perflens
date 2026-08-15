# PerfLens C Agent — Makefile
#
# Usage:
#   make                               # native build
#   make CROSS=aarch64-linux-gnu-      # ARM64 little-endian
#   make CROSS=aarch64_be-linux-musl-   # ARM64 big-endian (musl)
#   make CROSS=arm-linux-gnueabihf-    # ARMv7 32-bit little-endian
#   make CROSS=armeb-linux-musleabihf- # ARMv7 32-bit big-endian (musl)
#   make CROSS=x86_64-linux-musl-      # x86_64 with musl (fully static)
#   make all-cross                     # all cross targets
#   make clean

CC      = $(CROSS)gcc
VERSION := $(shell cat ../VERSION 2>/dev/null || echo dev)
CFLAGS  = -std=c99 -O2 -Wall -Wextra -Wno-unused-parameter \
          -D_POSIX_C_SOURCE=200809L -D_GNU_SOURCE \
          -DAGENT_VERSION=\"$(VERSION)\" -I.
LDFLAGS = -static -lpthread -lm

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

.PHONY: all clean all-cross print-bin

all: $(BIN)

print-bin:
	@echo $(BIN)

$(BIN): $(OBJS)
	@mkdir -p $(OUTDIR)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(LDFLAGS)
	@echo "Built: $@ ($(shell file $@ 2>/dev/null | grep -o 'statically linked' || echo 'check linking'))"

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
CROSS_TARGETS = aarch64-linux-gnu- aarch64_be-linux-musl- arm-linux-gnueabihf- armeb-linux-musleabihf- x86_64-linux-musl-

all-cross:
	@for cross in $(CROSS_TARGETS); do \
		arch=$$(echo $$cross | cut -d- -f1); \
		echo "=== Building for $$arch (CROSS=$$cross) ==="; \
		$(MAKE) CROSS=$$cross || echo "  SKIP: $$cross toolchain not found"; \
	done
	@echo "=== Cross builds complete ==="
	@ls -la build/*/$(TARGET) 2>/dev/null || true
