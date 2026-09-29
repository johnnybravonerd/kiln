# Benchmark: naive recursive Fibonacci (plain Python equivalent of fib32.kn)
def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

def main():
    print(f"fib(32) = {fib(32)}")

main()
