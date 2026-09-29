/* Benchmark: naive recursive Fibonacci, hand-written plain C (baseline for fib32.kn / fib40.kn).
   Usage: fib [N]   (default 32). Same algorithm and 64-bit integers as the Kiln version.
   Build: gcc -O3 -march=native -o fib fib.c */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static int64_t fib(int64_t n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}

int main(int argc, char **argv) {
    int64_t n = argc > 1 ? atoll(argv[1]) : 32;
    printf("fib(%lld) = %lld\n", (long long)n, (long long)fib(n));
    return 0;
}
