#!/usr/bin/env python3
"""Kiln benchmark runner.

Compares, for each benchmark (fib32, fib40, sieve10m):
  * Kiln default build (gcc, -O2, bounds checks on)
  * Kiln --release build (gcc, -O3 -march=native -flto, no bounds checks)
  * Kiln --release build with zig cc (skipped if zig is not found)
  * hand-written plain C (fib.c / sieve.c) at gcc -O3 -march=native
  * plain Python (CPython)
  * optionally the Kiln interpreter (--interp)

Times are wall-clock for the whole process, startup included. The table shows the best
and the median of N runs.

Usage: python3 bench/run_bench.py [--runs N] [--py-runs N] [--zig PATH] [--interp]
"""
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KILN = [sys.executable, os.path.join(os.path.dirname(HERE), "kiln.py")]
EXE = ".exe" if os.name == "nt" else ""

BENCHES = [
    # name, kiln source, python source, plain C source, plain C args
    ("fib32", "fib32.kn", "fib32.py", "fib.c", ["32"]),
    ("fib40", "fib40.kn", "fib40.py", "fib.c", ["40"]),
    ("sieve10m", "sieve10m.kn", "sieve10m.py", "sieve.c", []),
]


def arg(name, default):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def timed(cmd, runs):
    times, out = [], ""
    for _ in range(runs):
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True)
        times.append(time.perf_counter() - t0)
        if r.returncode != 0:
            raise SystemExit(f"command failed: {cmd}\n{r.stderr}")
        out = r.stdout.strip()
    return min(times), statistics.median(times), out


def kiln_build(src, exe, extra, env_cc=None):
    env = dict(os.environ)
    env.pop("KILN_CC", None)
    if env_cc:
        env["KILN_CC"] = env_cc
    r = subprocess.run(KILN + ["build", src, "-o", exe] + extra, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"kiln build failed: {src} {extra}\n{r.stderr}")


def main():
    runs = int(arg("--runs", "5"))
    py_runs = int(arg("--py-runs", "3"))
    zig = arg("--zig", os.environ.get("KILN_BENCH_ZIG") or shutil.which("zig"))
    interp = "--interp" in sys.argv
    tmp = tempfile.mkdtemp(prefix="kiln-bench-")
    rows = []
    for name, kn, py, c, cargs in BENCHES:
        src = os.path.join(HERE, kn)
        configs = [("Kiln default (gcc -O2, checks)", [], "gcc"),
                   ("Kiln --release (gcc)", ["--release"], "gcc")]
        if zig:
            configs.append(("Kiln --release (zig cc)", ["--release"], f"{zig} cc"))
        for i, (label, extra, cc) in enumerate(configs):
            exe = os.path.join(tmp, f"{name}-{i}{EXE}")
            kiln_build(src, exe, extra, cc)
            best, med, out = timed([exe], runs)
            rows.append((name, label, best, med, runs, out))
        cexe = os.path.join(tmp, f"{name}-c{EXE}")
        subprocess.run(["gcc", "-O3", "-march=native", "-o", cexe, os.path.join(HERE, c)], check=True)
        best, med, out = timed([cexe] + cargs, runs)
        rows.append((name, "plain C (gcc -O3 -march=native)", best, med, runs, out))
        best, med, out = timed([sys.executable, os.path.join(HERE, py)], py_runs)
        rows.append((name, f"Python {sys.version.split()[0]}", best, med, py_runs, out))
        if interp and name != "fib40":
            best, med, out = timed(KILN + ["run", "--interp", src], 1)
            rows.append((name, "Kiln interpreter (--interp)", best, med, 1, out))
    print(f"{'benchmark':<9} {'implementation':<34} {'best (s)':>9} {'median (s)':>11} {'runs':>5}  output")
    for name, impl, best, med, n, out in rows:
        print(f"{name:<9} {impl:<34} {best:>9.3f} {med:>11.3f} {n:>5}  {out}")


if __name__ == "__main__":
    main()
