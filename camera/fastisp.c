/* Native hot loops for the raw pipeline (rawisp.py loads this via ctypes).
 *
 *   gcc -O3 -march=native -fopenmp -shared -fPIC fastisp.c -o libfastisp.so
 *
 * Bayer layout is described by the caller:
 *   - per-phase white-balance gains gq[(y&1)*2 + (x&1)]   (Q8, 256 = 1.0)
 *   - g_diag: 1 when green sits at (0,0)/(1,1) (GB and GR patterns), 0 when at (0,1)/(1,0) (RG and BG)
 * Borders use reflect-101 (index -k -> k), which keeps the Bayer phase intact for even offsets.
 */
#include <stdint.h>
#include <stddef.h>
#ifdef _OPENMP
#include <omp.h>
#endif

void fastisp_set_threads(int n)
{
#ifdef _OPENMP
    omp_set_num_threads(n);
#else
    (void)n;
#endif
}

/* layout ids (must match camera/rawformat.py) */
enum { L_U8 = 0, L_U16 = 1, L_GEV12 = 2, L_GEV10 = 3, L_MIPI10 = 4 };

static inline int post(int p, int black16, int g)
{
    if (black16) { p -= black16; if (p < 0) p = 0; }
    if (g != 256) { p = (int)(((int64_t)p * g + 128) >> 8); if (p > 65535) p = 65535; }
    return p;
}

/* Decode one raw frame to uint16 at 16-bit scale (value << (16 - bits)), minus black16, times the phase gain.
 * Returns 0, or -1 for an unknown layout. */
int fastisp_unpack(const uint8_t *src, uint16_t *dst, int W, int H, int layout, int bits,
                   int black16, const int *gq)
{
    const int shift = 16 - bits;
    const int mask = (1 << bits) - 1;
    const int plain = (black16 == 0 && gq[0] == 256 && gq[1] == 256 && gq[2] == 256 && gq[3] == 256);
    switch (layout) {
    case L_U8: {
#pragma omp parallel for schedule(static)
        for (int y = 0; y < H; y++) {
            const uint8_t *s = src + (size_t)y * W;
            uint16_t *d = dst + (size_t)y * W;
            const int g0 = gq[(y & 1) * 2], g1 = gq[(y & 1) * 2 + 1];
            for (int x = 0; x < W; x += 2) {
                d[x]     = (uint16_t)post(s[x] << 8, black16, g0);
                d[x + 1] = (uint16_t)post(s[x + 1] << 8, black16, g1);
            }
        }
        return 0;
    }
    case L_U16: {
        const uint16_t *s16 = (const uint16_t *)src;           /* little-endian host assumed (ARM / x86) */
#pragma omp parallel for schedule(static)
        for (int y = 0; y < H; y++) {
            const uint16_t *s = s16 + (size_t)y * W;
            uint16_t *d = dst + (size_t)y * W;
            const int g0 = gq[(y & 1) * 2], g1 = gq[(y & 1) * 2 + 1];
            if (plain) {
                for (int x = 0; x < W; x++) d[x] = (uint16_t)((s[x] & mask) << shift);
            } else {
                for (int x = 0; x < W; x += 2) {
                    d[x]     = (uint16_t)post((s[x] & mask) << shift, black16, g0);
                    d[x + 1] = (uint16_t)post((s[x + 1] & mask) << shift, black16, g1);
                }
            }
        }
        return 0;
    }
    case L_GEV12: {
        const size_t stride = (size_t)W * 3 / 2;
#pragma omp parallel for schedule(static)
        for (int y = 0; y < H; y++) {
            const uint8_t *s = src + (size_t)y * stride;
            uint16_t *d = dst + (size_t)y * W;
            const int g0 = gq[(y & 1) * 2], g1 = gq[(y & 1) * 2 + 1];
            for (int x = 0; x < W; x += 2, s += 3) {
                const int p0 = (s[0] << 8) | ((s[1] & 15) << 4);
                const int p1 = (s[2] << 8) | ((s[1] >> 4) << 4);
                d[x]     = (uint16_t)(plain ? p0 : post(p0, black16, g0));
                d[x + 1] = (uint16_t)(plain ? p1 : post(p1, black16, g1));
            }
        }
        return 0;
    }
    case L_GEV10: {
        const size_t stride = (size_t)W * 3 / 2;
#pragma omp parallel for schedule(static)
        for (int y = 0; y < H; y++) {
            const uint8_t *s = src + (size_t)y * stride;
            uint16_t *d = dst + (size_t)y * W;
            const int g0 = gq[(y & 1) * 2], g1 = gq[(y & 1) * 2 + 1];
            for (int x = 0; x < W; x += 2, s += 3) {
                const int p0 = (s[0] << 8) | ((s[1] & 3) << 6);
                const int p1 = (s[2] << 8) | (((s[1] >> 2) & 3) << 6);
                d[x]     = (uint16_t)(plain ? p0 : post(p0, black16, g0));
                d[x + 1] = (uint16_t)(plain ? p1 : post(p1, black16, g1));
            }
        }
        return 0;
    }
    case L_MIPI10: {
        const size_t stride = (size_t)W * 5 / 4;
#pragma omp parallel for schedule(static)
        for (int y = 0; y < H; y++) {
            const uint8_t *s = src + (size_t)y * stride;
            uint16_t *d = dst + (size_t)y * W;
            const int g0 = gq[(y & 1) * 2], g1 = gq[(y & 1) * 2 + 1];
            for (int x = 0; x < W; x += 4, s += 5) {
                const int lo = s[4];
                const int p0 = (s[0] << 8) | ((lo & 3) << 6);
                const int p1 = (s[1] << 8) | (((lo >> 2) & 3) << 6);
                const int p2 = (s[2] << 8) | (((lo >> 4) & 3) << 6);
                const int p3 = (s[3] << 8) | (((lo >> 6) & 3) << 6);
                d[x]     = (uint16_t)(plain ? p0 : post(p0, black16, g0));
                d[x + 1] = (uint16_t)(plain ? p1 : post(p1, black16, g1));
                d[x + 2] = (uint16_t)(plain ? p2 : post(p2, black16, g0));
                d[x + 3] = (uint16_t)(plain ? p3 : post(p3, black16, g1));
            }
        }
        return 0;
    }
    default:
        return -1;
    }
}

static inline int refl(int i, int n)
{
    if (i < 0) i = -i;
    if (i >= n) i = 2 * (n - 1) - i;
    return i;
}

static inline int clip16(int v) { return v < 0 ? 0 : (v > 65535 ? 65535 : v); }

#define PX(yy, xx) ((int)src[(size_t)refl((yy), H) * W + refl((xx), W)])

/* SDK noise filter, fitted: green sites 36/64 c + 6/64 diag(+-1,+-1) + 1/64 axial(+-2);
 * red/blue sites 44/64 c + 4/64 axial(+-2) + 1/64 corner(+-2,+-2). */
static inline int nf_at(const uint16_t *src, int W, int H, int y, int x, int green, int interior)
{
    int c, a, b;
    if (interior) {
        const uint16_t *p = src + (size_t)y * W + x;
        c = p[0];
        if (green) {
            a = p[-W - 1] + p[-W + 1] + p[W - 1] + p[W + 1];                 /* diagonals +-1 */
            b = p[-2 * W] + p[2 * W] + p[-2] + p[2];                         /* axial +-2 */
            return (36 * c + 6 * a + b + 32) >> 6;
        }
        a = p[-2 * W] + p[2 * W] + p[-2] + p[2];
        b = p[-2 * W - 2] + p[-2 * W + 2] + p[2 * W - 2] + p[2 * W + 2];
        return (44 * c + 4 * a + b + 32) >> 6;
    }
    c = PX(y, x);
    if (green) {
        a = PX(y - 1, x - 1) + PX(y - 1, x + 1) + PX(y + 1, x - 1) + PX(y + 1, x + 1);
        b = PX(y - 2, x) + PX(y + 2, x) + PX(y, x - 2) + PX(y, x + 2);
        return (36 * c + 6 * a + b + 32) >> 6;
    }
    a = PX(y - 2, x) + PX(y + 2, x) + PX(y, x - 2) + PX(y, x + 2);
    b = PX(y - 2, x - 2) + PX(y - 2, x + 2) + PX(y + 2, x - 2) + PX(y + 2, x + 2);
    return (44 * c + 4 * a + b + 32) >> 6;
}

void fastisp_noise_filter(const uint16_t *src, uint16_t *dst, int W, int H, int g_diag)
{
#pragma omp parallel for schedule(static)
    for (int y = 0; y < H; y++) {
        const int row_int = (y >= 2 && y < H - 2);
        for (int x = 0; x < W; x++) {
            const int green = g_diag ? ((y & 1) == (x & 1)) : ((y & 1) != (x & 1));
            const int interior = row_int && x >= 2 && x < W - 2;
            dst[(size_t)y * W + x] = (uint16_t)nf_at(src, W, H, y, x, green, interior);
        }
    }
}

/* SDK sharpness, fitted: out = c + s * (4c - sum of the 4 same-colour neighbours 2 px away); sq = s in Q8.
 * The kernel is the same at every site, so it does not depend on the Bayer pattern. */
void fastisp_sharpen(const uint16_t *src, uint16_t *dst, int W, int H, int sq)
{
#pragma omp parallel for schedule(static)
    for (int y = 0; y < H; y++) {
        const int row_int = (y >= 2 && y < H - 2);
        for (int x = 0; x < W; x++) {
            int c, n;
            if (row_int && x >= 2 && x < W - 2) {
                const uint16_t *p = src + (size_t)y * W + x;
                c = p[0];
                n = p[-2 * W] + p[2 * W] + p[-2] + p[2];
            } else {
                c = PX(y, x);
                n = PX(y - 2, x) + PX(y + 2, x) + PX(y, x - 2) + PX(y, x + 2);
            }
            const int64_t lap = (int64_t)4 * c - n;
            int v = c + (int)((lap * sq + (lap >= 0 ? 128 : -128)) / 256);
            dst[(size_t)y * W + x] = (uint16_t)clip16(v);
        }
    }
}

/* In-place per-channel LUT on interleaved 3-channel 16-bit data; any table may be NULL (channel untouched). */
void fastisp_lut3(uint16_t *img, size_t npix, const uint16_t *tb, const uint16_t *tg, const uint16_t *tr)
{
#pragma omp parallel for schedule(static)
    for (long long i = 0; i < (long long)npix; i++) {
        uint16_t *p = img + (size_t)i * 3;
        if (tb) p[0] = tb[p[0]];
        if (tg) p[1] = tg[p[1]];
        if (tr) p[2] = tr[p[2]];
    }
}
