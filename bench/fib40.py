# Benchmark: naive recursive Fibonacci (plain Python equivalent of fib40.kn)
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

def main():
    print(f"fib(40) = {fib(40)}")

main()
