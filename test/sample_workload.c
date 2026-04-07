#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <unistd.h>

/* Heavy CPU work: tight loop with floating-point math */
void cpu_intensive(void) {
    volatile double result = 0.0;
    for (int i = 0; i < 5000000; i++) {
        result += sin((double)i) * cos((double)i) * tan((double)(i % 1000 + 1));
    }
}

/* Memory allocation and deallocation */
void memory_churner(void) {
    for (int i = 0; i < 10000; i++) {
        size_t size = (rand() % 4096) + 64;
        char *buf = malloc(size);
        if (buf) {
            memset(buf, (char)(i & 0xFF), size);
            free(buf);
        }
    }
}

/* String operations */
void string_worker(void) {
    char buf[1024];
    char tmp[1024];
    for (int i = 0; i < 200000; i++) {
        snprintf(buf, sizeof(buf), "iteration-%d-data-%d-value-%d", i, i * 7, i * 13);
        strcpy(tmp, buf);
        strcat(tmp, "-suffix");
        volatile size_t len = strlen(tmp);
        (void)len;
    }
}

/* Sorting work */
static int compare_int(const void *a, const void *b) {
    return (*(int *)a - *(int *)b);
}

void sorting_worker(void) {
    int arr[10000];
    for (int i = 0; i < 10000; i++) {
        arr[i] = rand();
    }
    for (int round = 0; round < 50; round++) {
        /* Shuffle */
        for (int i = 9999; i > 0; i--) {
            int j = rand() % (i + 1);
            int tmp = arr[i];
            arr[i] = arr[j];
            arr[j] = tmp;
        }
        qsort(arr, 10000, sizeof(int), compare_int);
    }
}

/* Orchestrator: calls all workers in a loop */
void run_workload(void) {
    printf("PerfLens sample workload running (PID: %d)\n", getpid());
    fflush(stdout);

    while (1) {
        cpu_intensive();
        memory_churner();
        string_worker();
        sorting_worker();
        usleep(10000); /* 10ms pause between iterations */
    }
}

int main(void) {
    srand(42);
    run_workload();
    return 0;
}
