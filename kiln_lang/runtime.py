"""The C runtime embedded at the top of every generated Kiln program.

Kept in portable C99 so it builds with gcc, clang, zig cc, tcc and mingw.
"""

RUNTIME_C = r'''
/* ---- Kiln runtime (C99) ---- */
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <setjmp.h>

typedef struct { const char *p; int64_t n; } kstr;
#define KS(lit, len) ((kstr){(lit), (int64_t)(len)})

static int k_argc; static char **k_argv;
#ifdef _WIN32
__declspec(dllimport) int __stdcall SetConsoleOutputCP(unsigned int cp);
#endif
static void k_init(int argc, char **argv) {
    k_argc = argc; k_argv = argv;
#ifdef _WIN32
    SetConsoleOutputCP(65001); /* UTF-8 console output */
#endif
}
static jmp_buf *k_test_jmp = NULL;
static char k_test_msg[512];

static void k_die(const char *prefix, const char *msg, int64_t n) {
    if (k_test_jmp) {
        snprintf(k_test_msg, sizeof k_test_msg, "%s%.*s", prefix, (int)n, msg);
        longjmp(*k_test_jmp, 1);
    }
    fflush(stdout);
    fprintf(stderr, "%s%.*s\n", prefix, (int)n, msg);
    exit(101);
}
static void k_panic_c(const char *msg) { k_die("panic: ", msg, (int64_t)strlen(msg)); }
static inline void k_panic(kstr msg) { k_die("panic: ", msg.p, msg.n); }
static inline void k_exit(int64_t code) { fflush(stdout); exit((int)code); }

static inline void *k_alloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) k_panic_c("out of memory");
    return p;
}
static void *k_realloc(void *q, size_t n) {
    void *p = realloc(q, n ? n : 1);
    if (!p) k_panic_c("out of memory");
    return p;
}

static int64_t k_index_fix(int64_t i, int64_t n) {
    int64_t j = i < 0 ? i + n : i;
    if (j < 0 || j >= n) {
        char buf[96];
        snprintf(buf, sizeof buf, "index %lld out of bounds (length %lld)", (long long)i, (long long)n);
        k_panic_c(buf);
    }
    return j;
}

/* Index checks. Default builds: negative indexes count from the end and anything
   out of range panics. Release builds (`kiln build --release` passes -DKILN_RELEASE
   and -DNDEBUG): no bounds check; only negative indexes are adjusted, then memory is
   accessed directly. Only KILN_RELEASE is tested here, because `zig cc -O2` defines
   NDEBUG on its own and default builds must keep their checks. */
#if defined(KILN_RELEASE)
#define KILN_BOUNDS_CHECKS 0
#define K_INDEX(i, n) do { if ((i) < 0) (i) += (n); } while (0)
#else
#define KILN_BOUNDS_CHECKS 1
#define K_INDEX(i, n) do { if ((uint64_t)(i) >= (uint64_t)(n)) (i) = k_index_fix((i), (n)); } while (0)
#endif

static inline int64_t k_div(int64_t a, int64_t b) {
    if (b == 0) k_panic_c("division by zero");
    if (b == -1) return (int64_t)(0 - (uint64_t)a);
    return a / b;
}
static inline int64_t k_mod(int64_t a, int64_t b) {
    if (b == 0) k_panic_c("division by zero");
    if (b == -1) return 0;
    return a % b;
}

/* ---- string builder ---- */
typedef struct { char *p; int64_t n, cap; } k_sb;
static void k_sb_init(k_sb *b) { b->cap = 32; b->n = 0; b->p = (char *)k_alloc(32); }
static void k_sb_addn(k_sb *b, const char *s, int64_t n) {
    if (b->n + n + 1 > b->cap) {
        while (b->n + n + 1 > b->cap) b->cap *= 2;
        b->p = (char *)k_realloc(b->p, (size_t)b->cap);
    }
    memcpy(b->p + b->n, s, (size_t)n);
    b->n += n;
}
static inline void k_sb_add(k_sb *b, kstr s) { k_sb_addn(b, s.p, s.n); }
static inline void k_sb_addc(k_sb *b, char c) { k_sb_addn(b, &c, 1); }
static inline kstr k_sb_done(k_sb *b) { b->p[b->n] = 0; return (kstr){b->p, b->n}; }

/* ---- conversions ---- */
static int k_fmt_i64(char *end, int64_t v) {
    char *p = end;
    uint64_t u = v < 0 ? 0 - (uint64_t)v : (uint64_t)v;
    do { *--p = (char)('0' + (int)(u % 10)); u /= 10; } while (u);
    if (v < 0) *--p = '-';
    return (int)(end - p);
}
static inline kstr k_str_dup(const char *s, int64_t n) {
    char *r = (char *)k_alloc((size_t)n + 1);
    memcpy(r, s, (size_t)n); r[n] = 0;
    return (kstr){r, n};
}
static kstr k_str_int(int64_t v) {
    char buf[24]; int n = k_fmt_i64(buf + 24, v);
    return k_str_dup(buf + 24 - n, n);
}
static int k_fmt_f64(char *buf, size_t cap, double v) {
    if (v != v) { strcpy(buf, "nan"); return 3; }
    if (v == HUGE_VAL) { strcpy(buf, "inf"); return 3; }
    if (v == -HUGE_VAL) { strcpy(buf, "-inf"); return 4; }
    int n = 0;
    for (int prec = 15; prec <= 17; prec++) {
        n = snprintf(buf, cap, "%.*g", prec, v);
        if (strtod(buf, NULL) == v) break;
    }
    if (!strpbrk(buf, ".eEn")) { buf[n++] = '.'; buf[n++] = '0'; buf[n] = 0; }
    return n;
}
static kstr k_str_float(double v) {
    char buf[48]; int n = k_fmt_f64(buf, sizeof buf, v);
    return k_str_dup(buf, n);
}
static inline kstr k_str_bool(bool v) { return v ? KS("true", 4) : KS("false", 5); }
static kstr k_repr_str(kstr s) {
    k_sb b; k_sb_init(&b); k_sb_addc(&b, '"');
    for (int64_t i = 0; i < s.n; i++) {
        char c = s.p[i];
        if (c == '"' || c == '\\') { k_sb_addc(&b, '\\'); k_sb_addc(&b, c); }
        else if (c == '\n') k_sb_addn(&b, "\\n", 2);
        else if (c == '\t') k_sb_addn(&b, "\\t", 2);
        else k_sb_addc(&b, c);
    }
    k_sb_addc(&b, '"');
    return k_sb_done(&b);
}

/* ---- printing ---- */
static inline void k_print_str(FILE *f, kstr s) { fwrite(s.p, 1, (size_t)s.n, f); }
static inline void k_print_lit(FILE *f, const char *s, size_t n) { fwrite(s, 1, n, f); }
static void k_print_int(FILE *f, int64_t v) {
    char buf[24]; int n = k_fmt_i64(buf + 24, v);
    fwrite(buf + 24 - n, 1, (size_t)n, f);
}
static void k_print_float(FILE *f, double v) {
    char buf[48]; int n = k_fmt_f64(buf, sizeof buf, v);
    fwrite(buf, 1, (size_t)n, f);
}
static inline void k_print_bool(FILE *f, bool v) { if (v) fwrite("true", 1, 4, f); else fwrite("false", 1, 5, f); }

/* ---- format specs: {x:>8} {x:.3f} {x:08} {x:x} ---- */
static kstr k_pad(kstr s, char fill, char align, int zero, int64_t width, int numeric) {
    if (s.n >= width) return s;
    int64_t pad = width - s.n;
    k_sb b; k_sb_init(&b);
    if (zero && !align) {
        int64_t i = 0;
        if (s.n && (s.p[0] == '-' || s.p[0] == '+')) { k_sb_addc(&b, s.p[0]); i = 1; }
        for (int64_t k = 0; k < pad; k++) k_sb_addc(&b, '0');
        k_sb_addn(&b, s.p + i, s.n - i);
        return k_sb_done(&b);
    }
    if (!align) align = numeric ? '>' : '<';
    int64_t left = align == '>' ? pad : align == '^' ? pad / 2 : 0;
    int64_t right = pad - left;
    for (int64_t k = 0; k < left; k++) k_sb_addc(&b, fill);
    k_sb_add(&b, s);
    for (int64_t k = 0; k < right; k++) k_sb_addc(&b, fill);
    return k_sb_done(&b);
}
static kstr k_fmt_int(int64_t v, char fill, char align, int zero, int64_t width, int prec, char type) {
    char buf[80]; kstr s;
    if (type == 'x' || type == 'X' || type == 'o' || type == 'b') {
        uint64_t u = v < 0 ? 0 - (uint64_t)v : (uint64_t)v;
        char *e = buf + 80, *p = e;
        int base = type == 'b' ? 2 : type == 'o' ? 8 : 16;
        const char *dig = type == 'X' ? "0123456789ABCDEF" : "0123456789abcdef";
        do { *--p = dig[u % (uint64_t)base]; u /= (uint64_t)base; } while (u);
        if (v < 0) *--p = '-';
        s = k_str_dup(p, e - p);
    } else if (type == 'f' || type == 'e' || prec >= 0) {
        int n = snprintf(buf, sizeof buf, type == 'e' ? "%.*e" : "%.*f", prec >= 0 ? prec : 6, (double)v);
        s = k_str_dup(buf, n);
    } else s = k_str_int(v);
    return k_pad(s, fill, align, zero, width, 1);
}
static kstr k_fmt_float(double v, char fill, char align, int zero, int64_t width, int prec, char type) {
    char buf[400]; kstr s;
    if (type == 'e') { int n = snprintf(buf, sizeof buf, "%.*e", prec >= 0 ? prec : 6, v); s = k_str_dup(buf, n); }
    else if (prec >= 0 || type == 'f') { int n = snprintf(buf, sizeof buf, "%.*f", prec >= 0 ? prec : 6, v); s = k_str_dup(buf, n); }
    else s = k_str_float(v);
    return k_pad(s, fill, align, zero, width, 1);
}
static kstr k_fmt_str(kstr s, char fill, char align, int zero, int64_t width, int prec, char type) {
    (void)type;
    if (prec >= 0 && s.n > prec) s.n = prec;
    return k_pad(s, fill, align, zero, width, 0);
}

/* ---- strings ---- */
static inline bool k_str_eq(kstr a, kstr b) { return a.n == b.n && (a.n == 0 || memcmp(a.p, b.p, (size_t)a.n) == 0); }
static inline bool k_str_eq_lit(kstr a, const char *b, int64_t n) { return a.n == n && (n == 0 || memcmp(a.p, b, (size_t)n) == 0); }
static inline int k_str_cmp(kstr a, kstr b) {
    int64_t n = a.n < b.n ? a.n : b.n;
    int c = n ? memcmp(a.p, b.p, (size_t)n) : 0;
    if (c) return c;
    return a.n < b.n ? -1 : a.n > b.n ? 1 : 0;
}
static inline kstr k_concat(kstr a, kstr b) {
    char *r = (char *)k_alloc((size_t)(a.n + b.n) + 1);
    memcpy(r, a.p, (size_t)a.n); memcpy(r + a.n, b.p, (size_t)b.n); r[a.n + b.n] = 0;
    return (kstr){r, a.n + b.n};
}
static kstr k_concatn(int n, const kstr *parts) {
    int64_t total = 0;
    for (int i = 0; i < n; i++) total += parts[i].n;
    char *r = (char *)k_alloc((size_t)total + 1), *p = r;
    for (int i = 0; i < n; i++) { memcpy(p, parts[i].p, (size_t)parts[i].n); p += parts[i].n; }
    *p = 0;
    return (kstr){r, total};
}
static inline kstr k_str_at(kstr s, int64_t i) {
    K_INDEX(i, s.n);
    return (kstr){s.p + i, 1};
}
static inline void k_slice_bounds(int64_t n, int64_t *a, int64_t *b) {
    if (*a < 0) *a += n;
    if (*b < 0) *b += n;
    if (KILN_BOUNDS_CHECKS && (*a < 0 || *b > n || *a > *b)) {
        char buf[128];
        snprintf(buf, sizeof buf, "slice %lld..%lld out of bounds (length %lld)", (long long)*a, (long long)*b, (long long)n);
        k_panic_c(buf);
    }
}
static inline kstr k_str_slice(kstr s, int64_t a, int64_t b) { k_slice_bounds(s.n, &a, &b); return (kstr){s.p + a, b - a}; }
static inline int64_t k_str_find(kstr s, kstr sub) {
    if (sub.n == 0) return 0;
    for (int64_t i = 0; i + sub.n <= s.n; i++)
        if (s.p[i] == sub.p[0] && memcmp(s.p + i, sub.p, (size_t)sub.n) == 0) return i;
    return -1;
}
static inline bool k_str_contains(kstr s, kstr sub) { return k_str_find(s, sub) >= 0; }
static inline bool k_str_starts_with(kstr s, kstr p) { return s.n >= p.n && memcmp(s.p, p.p, (size_t)p.n) == 0; }
static inline bool k_str_ends_with(kstr s, kstr p) { return s.n >= p.n && memcmp(s.p + s.n - p.n, p.p, (size_t)p.n) == 0; }
static kstr k_str_repeat(kstr s, int64_t n) {
    k_sb b; k_sb_init(&b);
    for (int64_t i = 0; i < n; i++) k_sb_add(&b, s);
    return k_sb_done(&b);
}
static kstr k_str_replace(kstr s, kstr from, kstr to) {
    if (from.n == 0) return s;
    k_sb b; k_sb_init(&b);
    int64_t i = 0;
    while (i < s.n) {
        if (i + from.n <= s.n && memcmp(s.p + i, from.p, (size_t)from.n) == 0) { k_sb_add(&b, to); i += from.n; }
        else k_sb_addc(&b, s.p[i++]);
    }
    return k_sb_done(&b);
}
static inline int k_isspace(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f'; }
static inline kstr k_str_trim(kstr s) {
    int64_t a = 0, b = s.n;
    while (a < b && k_isspace(s.p[a])) a++;
    while (b > a && k_isspace(s.p[b - 1])) b--;
    return (kstr){s.p + a, b - a};
}
static kstr k_str_case(kstr s, int upper) {
    char *r = (char *)k_alloc((size_t)s.n + 1);
    for (int64_t i = 0; i < s.n; i++) {
        char c = s.p[i];
        if (upper && c >= 'a' && c <= 'z') c = (char)(c - 32);
        else if (!upper && c >= 'A' && c <= 'Z') c = (char)(c + 32);
        r[i] = c;
    }
    r[s.n] = 0;
    return (kstr){r, s.n};
}
static int64_t k_parse_int(kstr s) {
    kstr t = k_str_trim(s);
    char buf[64];
    if (t.n == 0 || t.n >= 63) goto bad;
    memcpy(buf, t.p, (size_t)t.n); buf[t.n] = 0;
    {
        char *end; long long v = strtoll(buf, &end, 10);
        if (*end) goto bad;
        return (int64_t)v;
    }
bad:
    k_die("panic: parse_int: invalid integer: ", s.p, s.n);
    return 0;
}
static double k_parse_float(kstr s) {
    kstr t = k_str_trim(s);
    char buf[128];
    if (t.n == 0 || t.n >= 127) goto bad;
    memcpy(buf, t.p, (size_t)t.n); buf[t.n] = 0;
    {
        char *end; double v = strtod(buf, &end);
        if (*end) goto bad;
        return v;
    }
bad:
    k_die("panic: parse_float: invalid number: ", s.p, s.n);
    return 0;
}
static kstr k_read_line(void) {
    fflush(stdout);
    k_sb b; k_sb_init(&b);
    int c;
    while ((c = getchar()) != EOF && c != '\n') k_sb_addc(&b, (char)c);
    if (b.n && b.p[b.n - 1] == '\r') b.n--;
    return k_sb_done(&b);
}
static inline kstr k_ask(kstr prompt) { k_print_str(stdout, prompt); return k_read_line(); }
static kstr k_env(kstr name) {
    char buf[256];
    if (name.n >= 255) return KS("", 0);
    memcpy(buf, name.p, (size_t)name.n); buf[name.n] = 0;
    const char *v = getenv(buf);
    return v ? k_str_dup(v, (int64_t)strlen(v)) : KS("", 0);
}
static FILE *k_fopen(kstr path, const char *mode) {
    char *buf = (char *)k_alloc((size_t)path.n + 1);
    memcpy(buf, path.p, (size_t)path.n); buf[path.n] = 0;
    FILE *f = fopen(buf, mode);
    free(buf);
    return f;
}
static kstr k_read_text(kstr path) {
    FILE *f = k_fopen(path, "rb");
    if (!f) k_die("panic: fs.read_text: cannot open file: ", path.p, path.n);
    k_sb b; k_sb_init(&b);
    char chunk[65536]; size_t n;
    while ((n = fread(chunk, 1, sizeof chunk, f)) > 0) k_sb_addn(&b, chunk, (int64_t)n);
    fclose(f);
    return k_sb_done(&b);
}
static void k_write_text(kstr path, kstr text) {
    FILE *f = k_fopen(path, "wb");
    if (!f) k_die("panic: fs.write_text: cannot open file: ", path.p, path.n);
    fwrite(text.p, 1, (size_t)text.n, f);
    fclose(f);
}
static bool k_exists(kstr path) {
    FILE *f = k_fopen(path, "rb");
    if (f) { fclose(f); return true; }
    return false;
}
static int64_t k_now_ms(void) {
#if defined(TIME_UTC) && !defined(__TINYC__)
    struct timespec ts; timespec_get(&ts, TIME_UTC);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
#else
    return (int64_t)((double)clock() * 1000.0 / CLOCKS_PER_SEC);
#endif
}
static inline int64_t k_abs_i(int64_t v) { return v < 0 ? -v : v; }
static inline int64_t k_min_i(int64_t a, int64_t b) { return a < b ? a : b; }
static inline int64_t k_max_i(int64_t a, int64_t b) { return a > b ? a : b; }
static inline double k_min_f(double a, double b) { return a < b ? a : b; }
static inline double k_max_f(double a, double b) { return a > b ? a : b; }
static inline kstr k_min_s(kstr a, kstr b) { return k_str_cmp(a, b) <= 0 ? a : b; }
static inline kstr k_max_s(kstr a, kstr b) { return k_str_cmp(a, b) >= 0 ? a : b; }
static inline void k_assert(bool c, kstr msg, const char *where) {
    if (!c) {
        char buf[512];
        snprintf(buf, sizeof buf, "assertion failed at %s: %.*s", where, (int)msg.n, msg.p);
        k_die("", buf, (int64_t)strlen(buf));
    }
}

/* ---- dynamic arrays: one instantiation per element type ---- */
#define KARR_STRUCT(N, T) struct N { int64_t len, cap; T *data; };
#define KARR_FUNCS(N, T) \
static N *N##_new(int64_t cap) { \
    N *a = (N *)k_alloc(sizeof(N)); a->len = 0; a->cap = cap > 0 ? cap : 0; \
    a->data = cap > 0 ? (T *)k_alloc(sizeof(T) * (size_t)cap) : NULL; return a; } \
static void N##_grow(N *a) { \
    a->cap = a->cap ? a->cap * 2 : 4; a->data = (T *)k_realloc(a->data, sizeof(T) * (size_t)a->cap); } \
static inline void N##_push(N *a, T v) { if (a->len == a->cap) N##_grow(a); a->data[a->len++] = v; } \
static inline T *N##_at(N *a, int64_t i) { K_INDEX(i, a->len); return &a->data[i]; } \
static inline T N##_pop(N *a) { if (a->len == 0) k_panic_c("pop from empty list"); return a->data[--a->len]; } \
static inline void N##_clear(N *a) { a->len = 0; } \
static N *N##_from(int64_t n, T *src) { \
    N *a = N##_new(n); memcpy(a->data, src, sizeof(T) * (size_t)n); a->len = n; return a; } \
static N *N##_fill(int64_t n, T v) { \
    if (n < 0) { n = 0; } N *a = N##_new(n); for (int64_t i = 0; i < n; i++) a->data[i] = v; a->len = n; return a; } \
static inline N *N##_slice(N *a, int64_t s, int64_t e) { \
    k_slice_bounds(a->len, &s, &e); return N##_from(e - s, a->data + s); } \
static N *N##_reverse(N *a) { \
    N *r = N##_new(a->len); for (int64_t i = 0; i < a->len; i++) r->data[i] = a->data[a->len - 1 - i]; \
    r->len = a->len; return r; }

typedef struct karr_str karr_str;
KARR_STRUCT(karr_str, kstr)
KARR_FUNCS(karr_str, kstr)

static karr_str *k_str_split(kstr s, kstr sep) {
    karr_str *r = karr_str_new(0);
    if (sep.n == 0) { for (int64_t i = 0; i < s.n; i++) karr_str_push(r, (kstr){s.p + i, 1}); return r; }
    int64_t start = 0, i = 0;
    while (i + sep.n <= s.n) {
        if (memcmp(s.p + i, sep.p, (size_t)sep.n) == 0) {
            karr_str_push(r, (kstr){s.p + start, i - start}); i += sep.n; start = i;
        } else i++;
    }
    karr_str_push(r, (kstr){s.p + start, s.n - start});
    return r;
}
static karr_str *k_str_split_ws(kstr s) {
    karr_str *r = karr_str_new(0);
    int64_t i = 0;
    while (i < s.n) {
        while (i < s.n && k_isspace(s.p[i])) i++;
        int64_t st = i;
        while (i < s.n && !k_isspace(s.p[i])) i++;
        if (i > st) karr_str_push(r, (kstr){s.p + st, i - st});
    }
    return r;
}
static karr_str *k_str_lines(kstr s) {
    karr_str *r = karr_str_new(0);
    int64_t st = 0;
    for (int64_t i = 0; i < s.n; i++) {
        if (s.p[i] == '\n') {
            int64_t e = i; if (e > st && s.p[e - 1] == '\r') e--;
            karr_str_push(r, (kstr){s.p + st, e - st}); st = i + 1;
        }
    }
    if (st < s.n) karr_str_push(r, (kstr){s.p + st, s.n - st});
    return r;
}
static kstr k_str_join(karr_str *a, kstr sep) {
    k_sb b; k_sb_init(&b);
    for (int64_t i = 0; i < a->len; i++) { if (i) k_sb_add(&b, sep); k_sb_add(&b, a->data[i]); }
    return k_sb_done(&b);
}
static karr_str *k_make_args(void) {
    karr_str *r = karr_str_new(0);
    for (int i = 1; i < k_argc; i++) karr_str_push(r, k_str_dup(k_argv[i], (int64_t)strlen(k_argv[i])));
    return r;
}
static inline int k_cmp_int(const void *a, const void *b) { int64_t x = *(const int64_t *)a, y = *(const int64_t *)b; return (x > y) - (x < y); }
static inline int k_cmp_float(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }
static inline int k_cmp_str(const void *a, const void *b) { return k_str_cmp(*(const kstr *)a, *(const kstr *)b); }

/* ---- FFI helpers ---- */
static const char *k_cstr(kstr s) {
    char *r = (char *)k_alloc((size_t)s.n + 1);
    if (s.n) memcpy(r, s.p, (size_t)s.n);
    r[s.n] = 0;
    return r;
}
static kstr k_str_cstr(const char *p) {
    if (!p) return KS("(null)", 6);
    return k_str_dup(p, (int64_t)strlen(p));
}
static kstr k_str_ptr(void *p) {
    char buf[32];
    int n = snprintf(buf, sizeof buf, "%p", p);
    return k_str_dup(buf, n);
}
/* ---- end runtime ---- */
'''
