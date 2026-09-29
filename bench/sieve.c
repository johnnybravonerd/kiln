/* Benchmark: Sieve of Eratosthenes up to 10,000,000, hand-written plain C
   (baseline for sieve10m.kn). Same algorithm, one byte per flag, 64-bit indexes.
   Build: gcc -O3 -march=native -o sieve sieve.c */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    const int64_t n = 10000000;
    bool *is_prime = malloc((size_t)(n + 1) * sizeof(bool));
    if (!is_prime) return 1;
    for (int64_t k = 0; k <= n; k++) is_prime[k] = true;
    is_prime[0] = false;
    is_prime[1] = false;
    for (int64_t i = 2; i * i <= n; i++) {
        if (is_prime[i]) {
            for (int64_t j = i * i; j <= n; j += i) is_prime[j] = false;
        }
    }
    int64_t count = 0;
    for (int64_t k = 0; k <= n; k++) {
        if (is_prime[k]) count++;
    }
    printf("primes up to %lld: %lld\n", (long long)n, (long long)count);
    free(is_prime);
    return 0;
}
