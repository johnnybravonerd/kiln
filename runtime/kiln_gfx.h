/* Kiln graphics backend (Win32 GDI + X11).
 *
 * The public Kiln API lives in lib/gfx.kn. This C shim owns the OS window,
 * message/event pump, keyboard state, and blitting a software U32 pixel buffer
 * (0xAARRGGBB / 0x00RRGGBB — alpha ignored) via StretchDIBits (Windows) or
 * XPutImage (X11).
 *
 * Link with @link("kiln_gfx") from Kiln; the driver compiles runtime/kiln_gfx.c
 * and adds -lX11 (Linux) or -luser32 -lgdi32 (Windows).
 */
#ifndef KILN_GFX_H
#define KILN_GFX_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Open a window. Logical size is w×h pixels; scale is integer window scale (≥1).
   Returns 1 on success, 0 on failure. */
int kgfx_open(const char *title, int32_t w, int32_t h, int32_t scale);

/* Poll OS events. Returns 1 while the window is open, 0 after close. */
int kgfx_running(void);

/* Blit pixels (len >= w*h, row-major, 0x00RRGGBB) to the window. */
void kgfx_present(const void *pixels, int32_t w, int32_t h);

/* Keyboard: key is a small integer (see gfx.kn KEY_*). down = currently held;
   pressed = edge since last poll (consumed on read). */
int kgfx_key_down(int32_t key);
int kgfx_key_pressed(int32_t key);

int64_t kgfx_time_ms(void);
void kgfx_sleep_ms(int32_t ms);
void kgfx_close(void);

#ifdef __cplusplus
}
#endif
#endif
