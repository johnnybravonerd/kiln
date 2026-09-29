/* Kiln graphics OS backend — see kiln_gfx.h for the contract. */
#include "kiln_gfx.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <X11/Xlib.h>
#  include <X11/Xutil.h>
#  include <X11/keysym.h>
#  include <unistd.h>
#  include <time.h>
#  include <sys/time.h>
#endif

enum {
    KGFX_MAX_KEYS = 512,
};

static int g_open = 0;
static int g_running = 0;
static int32_t g_w = 0, g_h = 0, g_scale = 1;
static int g_key_down[KGFX_MAX_KEYS];
static int g_key_pressed[KGFX_MAX_KEYS];

static void kgfx_clear_keys(void) {
    memset(g_key_down, 0, sizeof g_key_down);
    memset(g_key_pressed, 0, sizeof g_key_pressed);
}

static void kgfx_set_key(int code, int down) {
    if (code < 0 || code >= KGFX_MAX_KEYS) return;
    if (down) {
        if (!g_key_down[code]) g_key_pressed[code] = 1;
        g_key_down[code] = 1;
    } else {
        g_key_down[code] = 0;
    }
}

/* ---- keycode mapping (Kiln KEY_* integers) ----
   0-9 digits, 10-35 A-Z, 36 left, 37 right, 38 up, 39 down,
   40 space, 41 escape, 42 enter, 43 shift, 44 ctrl, 45 tab
*/
static int map_key_win(int vk) {
#if defined(_WIN32)
    if (vk >= '0' && vk <= '9') return vk - '0';
    if (vk >= 'A' && vk <= 'Z') return 10 + (vk - 'A');
    switch (vk) {
        case VK_LEFT: return 36; case VK_RIGHT: return 37;
        case VK_UP: return 38; case VK_DOWN: return 39;
        case VK_SPACE: return 40; case VK_ESCAPE: return 41;
        case VK_RETURN: return 42;
        case VK_SHIFT: case VK_LSHIFT: case VK_RSHIFT: return 43;
        case VK_CONTROL: case VK_LCONTROL: case VK_RCONTROL: return 44;
        case VK_TAB: return 45;
        default: return -1;
    }
#else
    (void)vk; return -1;
#endif
}

#if !defined(_WIN32)
static int map_key_x11(KeySym ks) {
    if (ks >= XK_0 && ks <= XK_9) return (int)(ks - XK_0);
    if (ks >= XK_a && ks <= XK_z) return 10 + (int)(ks - XK_a);
    if (ks >= XK_A && ks <= XK_Z) return 10 + (int)(ks - XK_A);
    switch (ks) {
        case XK_Left: return 36; case XK_Right: return 37;
        case XK_Up: return 38; case XK_Down: return 39;
        case XK_space: return 40; case XK_Escape: return 41;
        case XK_Return: return 42;
        case XK_Shift_L: case XK_Shift_R: return 43;
        case XK_Control_L: case XK_Control_R: return 44;
        case XK_Tab: return 45;
        default: return -1;
    }
}
#endif

#if defined(_WIN32)
/* ======================== Win32 ======================== */
static HWND g_hwnd = NULL;
static HBITMAP g_dib = NULL;
static void *g_dib_bits = NULL;
static int32_t g_dib_w = 0, g_dib_h = 0;
static BITMAPINFO g_bmi;
static const char *g_cls = "KilnGfxWindow";

static void ensure_dib(int32_t w, int32_t h) {
    if (g_dib && g_dib_w == w && g_dib_h == h) return;
    if (g_dib) { DeleteObject(g_dib); g_dib = NULL; g_dib_bits = NULL; }
    memset(&g_bmi, 0, sizeof g_bmi);
    g_bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    g_bmi.bmiHeader.biWidth = w;
    g_bmi.bmiHeader.biHeight = -h; /* top-down */
    g_bmi.bmiHeader.biPlanes = 1;
    g_bmi.bmiHeader.biBitCount = 32;
    g_bmi.bmiHeader.biCompression = BI_RGB;
    HDC hdc = GetDC(g_hwnd);
    g_dib = CreateDIBSection(hdc, &g_bmi, DIB_RGB_COLORS, &g_dib_bits, NULL, 0);
    ReleaseDC(g_hwnd, hdc);
    g_dib_w = w; g_dib_h = h;
}

static LRESULT CALLBACK kgfx_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_CLOSE:
            DestroyWindow(hwnd);
            return 0;
        case WM_DESTROY:
            g_running = 0;
            PostQuitMessage(0);
            return 0;
        case WM_KEYDOWN: case WM_SYSKEYDOWN: {
            int k = map_key_win((int)wp);
            if (k >= 0) kgfx_set_key(k, 1);
            return 0;
        }
        case WM_KEYUP: case WM_SYSKEYUP: {
            int k = map_key_win((int)wp);
            if (k >= 0) kgfx_set_key(k, 0);
            return 0;
        }
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(hwnd, &ps);
            if (g_dib_bits) {
                StretchDIBits(hdc, 0, 0, g_w * g_scale, g_h * g_scale,
                              0, 0, g_dib_w, g_dib_h, g_dib_bits, &g_bmi, DIB_RGB_COLORS, SRCCOPY);
            }
            EndPaint(hwnd, &ps);
            return 0;
        }
        default:
            return DefWindowProcA(hwnd, msg, wp, lp);
    }
}

int kgfx_open(const char *title, int32_t w, int32_t h, int32_t scale) {
    if (g_open) kgfx_close();
    if (w < 1 || h < 1) return 0;
    if (scale < 1) scale = 1;
    g_w = w; g_h = h; g_scale = scale;
    kgfx_clear_keys();

    WNDCLASSA wc;
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = kgfx_wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = g_cls;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    RegisterClassA(&wc);

    DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    RECT r = {0, 0, w * scale, h * scale};
    AdjustWindowRect(&r, style, FALSE);
    g_hwnd = CreateWindowA(g_cls, title ? title : "Kiln", style,
                           CW_USEDEFAULT, CW_USEDEFAULT,
                           r.right - r.left, r.bottom - r.top,
                           NULL, NULL, GetModuleHandleA(NULL), NULL);
    if (!g_hwnd) return 0;
    ensure_dib(w, h);
    ShowWindow(g_hwnd, SW_SHOW);
    UpdateWindow(g_hwnd);
    g_open = 1;
    g_running = 1;
    return 1;
}

static void pump(void) {
    MSG msg;
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
        if (msg.message == WM_QUIT) { g_running = 0; break; }
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
}

int kgfx_running(void) {
    if (!g_open) return 0;
    /* clear edge flags each poll cycle after reads — callers use key_pressed once per frame */
    pump();
    return g_running;
}

void kgfx_present(const void *pixels, int32_t w, int32_t h) {
    if (!g_open || !pixels) return;
    ensure_dib(w, h);
    /* Kiln buffer is 0x00RRGGBB; Windows DIB wants BGRX in memory on little-endian. */
    const uint32_t *src = (const uint32_t *)pixels;
    uint32_t *dst = (uint32_t *)g_dib_bits;
    int32_t n = w * h;
    for (int32_t i = 0; i < n; i++) {
        uint32_t c = src[i];
        uint32_t r = (c >> 16) & 0xFF;
        uint32_t g = (c >> 8) & 0xFF;
        uint32_t b = c & 0xFF;
        dst[i] = (b) | (g << 8) | (r << 16);
    }
    InvalidateRect(g_hwnd, NULL, FALSE);
    pump();
    HDC hdc = GetDC(g_hwnd);
    StretchDIBits(hdc, 0, 0, g_w * g_scale, g_h * g_scale,
                  0, 0, w, h, g_dib_bits, &g_bmi, DIB_RGB_COLORS, SRCCOPY);
    ReleaseDC(g_hwnd, hdc);
}

void kgfx_close(void) {
    if (g_dib) { DeleteObject(g_dib); g_dib = NULL; g_dib_bits = NULL; }
    if (g_hwnd) { DestroyWindow(g_hwnd); g_hwnd = NULL; }
    g_open = 0; g_running = 0;
}

int64_t kgfx_time_ms(void) { return (int64_t)GetTickCount64(); }
void kgfx_sleep_ms(int32_t ms) { if (ms > 0) Sleep((DWORD)ms); }

#else
/* ======================== X11 ======================== */
static Display *g_dpy = NULL;
static Window g_win = 0;
static GC g_gc = 0;
static XImage *g_img = NULL;
static uint32_t *g_img_buf = NULL;
static int32_t g_img_w = 0, g_img_h = 0;
static Atom g_wm_delete;

static void free_img(void) {
    if (g_img) { g_img->data = NULL; XDestroyImage(g_img); g_img = NULL; }
    free(g_img_buf); g_img_buf = NULL;
    g_img_w = g_img_h = 0;
}

static int ensure_img(int32_t w, int32_t h) {
    if (g_img && g_img_w == w && g_img_h == h) return 1;
    free_img();
    g_img_buf = (uint32_t *)malloc((size_t)w * (size_t)h * 4);
    if (!g_img_buf) return 0;
    memset(g_img_buf, 0, (size_t)w * (size_t)h * 4);
    int screen = DefaultScreen(g_dpy);
    g_img = XCreateImage(g_dpy, DefaultVisual(g_dpy, screen), 24, ZPixmap, 0,
                         (char *)g_img_buf, (unsigned)w, (unsigned)h, 32, 0);
    if (!g_img) { free(g_img_buf); g_img_buf = NULL; return 0; }
    g_img_w = w; g_img_h = h;
    return 1;
}

int kgfx_open(const char *title, int32_t w, int32_t h, int32_t scale) {
    if (g_open) kgfx_close();
    if (w < 1 || h < 1) return 0;
    if (scale < 1) scale = 1;
    g_w = w; g_h = h; g_scale = scale;
    kgfx_clear_keys();

    g_dpy = XOpenDisplay(NULL);
    if (!g_dpy) {
        fprintf(stderr, "kiln_gfx: cannot open X11 display (is DISPLAY set?)\n");
        return 0;
    }
    int screen = DefaultScreen(g_dpy);
    unsigned long black = BlackPixel(g_dpy, screen);
    g_win = XCreateSimpleWindow(g_dpy, RootWindow(g_dpy, screen),
                                64, 64, (unsigned)(w * scale), (unsigned)(h * scale),
                                1, black, black);
    XStoreName(g_dpy, g_win, title ? title : "Kiln");
    XSelectInput(g_dpy, g_win, ExposureMask | KeyPressMask | KeyReleaseMask | StructureNotifyMask);
    g_wm_delete = XInternAtom(g_dpy, "WM_DELETE_WINDOW", False);
    XSetWMProtocols(g_dpy, g_win, &g_wm_delete, 1);
    g_gc = XCreateGC(g_dpy, g_win, 0, NULL);
    if (!ensure_img(w, h)) { kgfx_close(); return 0; }
    XMapWindow(g_dpy, g_win);
    XFlush(g_dpy);
    g_open = 1;
    g_running = 1;
    return 1;
}

static void pump(void) {
    while (g_dpy && XPending(g_dpy)) {
        XEvent ev;
        XNextEvent(g_dpy, &ev);
        if (ev.type == ClientMessage) {
            if ((Atom)ev.xclient.data.l[0] == g_wm_delete) g_running = 0;
        } else if (ev.type == DestroyNotify) {
            g_running = 0;
        } else if (ev.type == KeyPress || ev.type == KeyRelease) {
            KeySym ks = XLookupKeysym(&ev.xkey, 0);
            int k = map_key_x11(ks);
            if (k >= 0) kgfx_set_key(k, ev.type == KeyPress);
        }
    }
}

int kgfx_running(void) {
    if (!g_open) return 0;
    pump();
    return g_running;
}

void kgfx_present(const void *pixels, int32_t w, int32_t h) {
    if (!g_open || !pixels || !g_dpy) return;
    if (!ensure_img(w, h)) return;
    const uint32_t *src = (const uint32_t *)pixels;
    /* XImage 24/32 on little-endian typically wants 0x00BBGGRR in memory for TrueColor,
       but many modern servers accept 0x00RRGGBB when red_mask is 0xff0000. Copy as RRGGBB. */
    memcpy(g_img_buf, src, (size_t)w * (size_t)h * 4);
    if (g_scale == 1) {
        XPutImage(g_dpy, g_win, g_gc, g_img, 0, 0, 0, 0, (unsigned)w, (unsigned)h);
    } else {
        /* Nearest-neighbor scale into a temporary — for simplicity, put then let WM size;
           better: draw scaled. Use XPutImage at 1:1 into a pixmap and XCopyArea scaled is hard
           without XRender; do software scale. */
        int32_t dw = w * g_scale, dh = h * g_scale;
        uint32_t *scaled = (uint32_t *)malloc((size_t)dw * (size_t)dh * 4);
        if (scaled) {
            for (int32_t y = 0; y < dh; y++) {
                int32_t sy = y / g_scale;
                for (int32_t x = 0; x < dw; x++) {
                    int32_t sx = x / g_scale;
                    scaled[y * dw + x] = src[sy * w + sx];
                }
            }
            XImage *img2 = XCreateImage(g_dpy, DefaultVisual(g_dpy, DefaultScreen(g_dpy)), 24, ZPixmap, 0,
                                        (char *)scaled, (unsigned)dw, (unsigned)dh, 32, 0);
            if (img2) {
                XPutImage(g_dpy, g_win, g_gc, img2, 0, 0, 0, 0, (unsigned)dw, (unsigned)dh);
                img2->data = NULL;
                XDestroyImage(img2);
            }
            free(scaled);
        }
    }
    XFlush(g_dpy);
    pump();
}

void kgfx_close(void) {
    free_img();
    if (g_dpy) {
        if (g_gc) { XFreeGC(g_dpy, g_gc); g_gc = 0; }
        if (g_win) { XDestroyWindow(g_dpy, g_win); g_win = 0; }
        XCloseDisplay(g_dpy);
        g_dpy = NULL;
    }
    g_open = 0; g_running = 0;
}

int64_t kgfx_time_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

void kgfx_sleep_ms(int32_t ms) {
    if (ms <= 0) return;
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}
#endif

int kgfx_key_down(int32_t key) {
    if (key < 0 || key >= KGFX_MAX_KEYS) return 0;
    return g_key_down[key];
}

int kgfx_key_pressed(int32_t key) {
    if (key < 0 || key >= KGFX_MAX_KEYS) return 0;
    int v = g_key_pressed[key];
    g_key_pressed[key] = 0;
    return v;
}
