CC = gcc
CFLAGS = -g -O0 -Wall
LDFLAGS = -lm

all: sample_workload

sample_workload: sample_workload.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f sample_workload

.PHONY: all clean
