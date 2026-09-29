# Benchmark: Sieve of Eratosthenes up to 10,000,000 (plain Python equivalent of sieve10m.kn)
def main():
    n = 10_000_000
    is_prime = [True] * (n + 1)
    is_prime[0] = False
    is_prime[1] = False
    i = 2
    while i * i <= n:
        if is_prime[i]:
            j = i * i
            while j <= n:
                is_prime[j] = False
                j += i
        i += 1
    count = 0
    for k in range(0, n + 1):
        if is_prime[k]:
            count += 1
    print(f"primes up to {n}: {count}")

main()
