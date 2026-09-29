#!/usr/bin/env bash
# Kiln test suite: builds every example with the C backend, runs it, and compares
# stdout with tests/expected/<name>.out. Also checks the interpreter (--interp)
# gives identical output, that compile errors report the right line:column,
# that `kiln test` runs test blocks, and that the REPL transcript matches.
#
# Usage: tests/run_tests.sh            (set KILN_CC to pick a C compiler)
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KILN="$ROOT/kiln"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "== examples (compiled with the C backend)"
cd "$ROOT/examples"
for src in *.kn; do
  name="${src%.kn}"
  # Interactive / display demos are exercised separately
  case "$name" in gfx_demo|snake|ffi_msgbox) continue ;; esac
  exp="$ROOT/tests/expected/$name.out"
  [ -f "$exp" ] || { bad "$name (no expected output)"; continue; }
  args=""
  [ -f "$ROOT/tests/expected/$name.args" ] && args="$(cat "$ROOT/tests/expected/$name.args")"
  if ! "$KILN" build "$src" -o "$TMP/$name" > "$TMP/$name.build" 2>&1; then
    bad "$name (build failed)"; cat "$TMP/$name.build"; continue
  fi
  # shellcheck disable=SC2086
  "$TMP/$name" $args > "$TMP/$name.out" 2>/dev/null
  if diff -u "$exp" "$TMP/$name.out" > "$TMP/$name.diff"; then ok "$name"; else bad "$name (output differs)"; cat "$TMP/$name.diff"; fi
done

echo "== examples (--release: -O3 -march=native -flto, no bounds checks)"
for src in *.kn; do
  name="${src%.kn}"
  case "$name" in gfx_demo|snake|ffi_msgbox) continue ;; esac
  args=""
  [ -f "$ROOT/tests/expected/$name.args" ] && args="$(cat "$ROOT/tests/expected/$name.args")"
  if ! "$KILN" build "$src" --release -o "$TMP/$name.rel" > "$TMP/$name.rbuild" 2>&1; then
    bad "$name --release (build failed)"; cat "$TMP/$name.rbuild"; continue
  fi
  # shellcheck disable=SC2086
  "$TMP/$name.rel" $args > "$TMP/$name.rout" 2>/dev/null
  if diff -u "$ROOT/tests/expected/$name.out" "$TMP/$name.rout" > "$TMP/$name.rdiff"; then ok "$name --release"; else bad "$name --release"; cat "$TMP/$name.rdiff"; fi
done

echo "== runtime bounds checks"
B="$ROOT/tests/runtime/bounds.kn"
"$KILN" build "$B" -o "$TMP/bounds" > /dev/null 2>&1
"$TMP/bounds" > "$TMP/bounds.out" 2> "$TMP/bounds.err"; rc=$?
if [ $rc -eq 101 ] && grep -q "last 3 slice \[2, 3\] str n" "$TMP/bounds.out" && grep -q "panic: index 7 out of bounds (length 3)" "$TMP/bounds.err"; then
  ok "default build panics on out-of-bounds index (exit 101)"; else bad "default build bounds check (rc=$rc)"; cat "$TMP/bounds.out" "$TMP/bounds.err"; fi
"$KILN" build "$B" --release -o "$TMP/bounds.rel" > /dev/null 2>&1
"$TMP/bounds.rel" 2>/dev/null | head -1 > "$TMP/bounds.rout"
if grep -q "last 3 slice \[2, 3\] str n" "$TMP/bounds.rout"; then ok "release build keeps negative indexes"; else bad "release negative indexes"; cat "$TMP/bounds.rout"; fi
if "$KILN" emit-c "$B" | grep -q "define K_INDEX(i, n) do { if ((i) < 0) (i) += (n); } while (0)"; then ok "release K_INDEX macro is direct access"; else bad "release K_INDEX macro"; fi

echo "== examples (interpreter, must match the compiled output)"
for src in *.kn; do
  name="${src%.kn}"
  case "$name" in gfx_demo|snake|ffi_msgbox|ffi_hello) continue ;; esac
  args=""
  [ -f "$ROOT/tests/expected/$name.args" ] && args="$(cat "$ROOT/tests/expected/$name.args")"
  # shellcheck disable=SC2086
  "$KILN" run --interp "$src" $args > "$TMP/$name.iout" 2>/dev/null
  if diff -u "$ROOT/tests/expected/$name.out" "$TMP/$name.iout" > "$TMP/$name.idiff"; then ok "$name --interp"; else bad "$name --interp"; cat "$TMP/$name.idiff"; fi
done

echo "== kiln test (test blocks)"
if "$KILN" test wordcount.kn > "$TMP/test.out" 2>&1 && grep -q "2 passed, 0 failed" "$TMP/test.out"; then ok "kiln test wordcount.kn"; else bad "kiln test wordcount.kn"; cat "$TMP/test.out"; fi

echo "== compile errors (line:column + message)"
cd "$ROOT/tests/errors"
for src in *.kn; do
  name="${src%.kn}"
  want="$(cat "$name.expect")"
  out="$("$KILN" check "$src" 2>&1)"; rc=$?
  if [ $rc -ne 0 ] && grep -qF -- "$src:$want" <<< "$out"; then ok "error: $name"; else bad "error: $name"; echo "    want: $src:$want"; echo "    got:  $out"; fi
done

echo "== REPL transcript"
"$KILN" repl < "$ROOT/tests/repl_session.txt" > "$TMP/repl.out" 2>&1
if diff -u "$ROOT/tests/repl_expected.txt" "$TMP/repl.out" > "$TMP/repl.diff"; then ok "repl session"; else bad "repl session"; cat "$TMP/repl.diff"; fi

echo "== modules"
cd "$ROOT/tests/modules"
if "$KILN" build use_math.kn -o "$TMP/use_math" > "$TMP/use_math.build" 2>&1 && \
   "$TMP/use_math" > "$TMP/use_math.out" 2>/dev/null && \
   diff -u use_math.out "$TMP/use_math.out" > "$TMP/use_math.diff"; then
  ok "use mathutil module"
else bad "use mathutil module"; cat "$TMP/use_math.build" "$TMP/use_math.diff" 2>/dev/null; fi
cyc="$("$KILN" check cycle_main.kn 2>&1)"; rc=$?
if [ $rc -ne 0 ] && grep -q "circular module dependency" <<< "$cyc"; then ok "module cycle detection"
else bad "module cycle detection"; echo "$cyc"; fi
priv="$("$KILN" check - 2>&1 << 'KN' || true
use mathutil
fn main() { println("{mathutil.private_helper(1)}") }
KN
)"
# private access via a tiny file
cp mathutil.kn "$TMP/mathutil.kn"
cat > "$TMP/priv.kn" << 'KN'
use mathutil
fn main() { println("{mathutil.private_helper(1)}") }
KN
priv="$("$KILN" check "$TMP/priv.kn" 2>&1)"; rc=$?
if [ $rc -ne 0 ] && grep -qiE "not \`pub\`|no public" <<< "$priv"; then ok "non-pub module item rejected"
else bad "non-pub module item rejected"; echo "$priv"; fi

echo "== FFI / sized ints"
cd "$ROOT/tests/ffi"
if "$KILN" build sized.kn -o "$TMP/sized" > "$TMP/sized.build" 2>&1 && \
   "$TMP/sized" > "$TMP/sized.out" && diff -u sized.out "$TMP/sized.out" > "$TMP/sized.diff"; then
  ok "sized ints + null Ptr"
else bad "sized ints + null Ptr"; cat "$TMP/sized.build" "$TMP/sized.diff" 2>/dev/null; fi

echo "== gfx smoke (X11, short run)"
if [ -n "${DISPLAY:-}" ]; then
  if KILN_GFX_FRAMES=15 "$KILN" run "$ROOT/examples/gfx_demo.kn" > "$TMP/gfx.out" 2>"$TMP/gfx.err"; then ok "gfx_demo short run"
  else bad "gfx_demo short run"; cat "$TMP/gfx.err"; fi
else
  echo "  SKIP  gfx_demo (no DISPLAY)"
fi

echo "== emit-c produces self-contained C"
if "$KILN" emit-c "$ROOT/examples/hello.kn" > "$TMP/hello.c" && grep -q "int main" "$TMP/hello.c"; then ok "emit-c"; else bad "emit-c"; fi

echo
echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
