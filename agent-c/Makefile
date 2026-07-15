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
          -DAGENT_VERSION=\"$(VERSION)\"
LDFLAGS = -static -lpthread -lm

TARGET  = perflens-agent
SRCS    = perflens_agent.c vendor/zstd/zstd.c
OBJS    = perflens_agent.o vendor/zstd/zstd.o

.PHONY: all clean all-cross

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(LDFLAGS)
	@echo "Built: $@ ($(shell file $@ 2>/dev/null | grep -o 'statically linked' || echo 'check linking'))"

perflens_agent.o: perflens_agent.c
	$(CC) $(CFLAGS) -c -o $@ $<

vendor/zstd/zstd.o: vendor/zstd/zstd.c vendor/zstd/zstd.h
	$(CC) $(CFLAGS) -DZSTD_DISABLE_ASM -c -o $@ $<

clean:
	rm -f $(TARGET) $(OBJS)

# Cross-compile all supported targets — output into build/<arch>/
CROSS_TARGETS = aarch64-linux-gnu- aarch64_be-linux-musl- arm-linux-gnueabihf- armeb-linux-musleabihf- x86_64-linux-musl-

all-cross:
	@for cross in $(CROSS_TARGETS); do \
		arch=$$(echo $$cross | cut -d- -f1); \
		echo "=== Building for $$arch (CROSS=$$cross) ==="; \
		mkdir -p build/$$arch; \
		$(MAKE) clean 2>/dev/null; \
		$(MAKE) CROSS=$$cross && cp $(TARGET) build/$$arch/ || \
			echo "  SKIP: $$cross toolchain not found"; \
	done
	@echo "=== Cross builds complete ==="
	@ls -la build/*/$(TARGET) 2>/dev/null || true
