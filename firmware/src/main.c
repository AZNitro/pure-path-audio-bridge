/*
 * Phase 2A: Linux -> lpuart1 (DMA) -> frames -> PCM ring -> SAI2 (DMA) -> PCM5102
 *
 * Frame: [A5 5A][seq u16][type u8][fmt u8][len u16][payload len][crc16 u16]
 *   type: 1 AUDIO, 2 CTRL, 3 STATUS (STM32->Linux)
 *   fmt : bits[7:4] width (1=16,2=24)  bits[3:0] rate (1=44.1k, 2=48k)
 *   crc : CRC-16/CCITT-FALSE over seq..payload
 *
 * STATUS payload (STM32->Linux, every 100 ms), all u32 LE:
 *   fill_bytes, ring_size, underruns, bad, drop, rx_overflow,
 *   pcm_crc32, consumed_bytes            (32 bytes; was 24 before Phase 5)
 *
 * pcm_crc32 is CRC-32 (zlib polynomial) over every PCM byte actually handed
 * to the SAI from the ring -- silence inserted on underrun is excluded. The
 * sender computes the same over what it wrote; equal CRCs with equal byte
 * counts prove the path is bit-perfect.
 *
 * SAI runs from boot and never stops. Silence until the PCM ring first reaches
 * 50 % (primed); on underrun, silence + re-prime. Clock never drops -> no clicks.
 *
 * Matrix (13x8, 3-bit grayscale, driver self-refreshes from the framebuffer),
 * drawn at 20 fps from the main loop. Three exclusive modes, highest first:
 *   DIAG      row0 bad  row1 drop  row2 underrun  row3 rx_ovf  row7 ring fill
 *   VU        rows 1-2 = left, rows 5-6 = right: RMS over the 50 ms draw
 *             interval, shown RELATIVE to an auto-gain reference (see below),
 *             plus a 1-pixel peak hold of the bar that decays 1 px / 300 ms
 *   HEARTBEAT one dim pixel, blipping every 2 s: booted but nothing playing
 *
 * The level is taken in the feeder thread over the same bytes that feed the CRC
 * -- i.e. what actually reaches the SAI. Nothing is added to any ISR or DMA
 * callback, and the STATUS payload is unchanged at 32 bytes.
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/i2s.h>
#include <zephyr/drivers/display.h>
#include <zephyr/sys/ring_buffer.h>
#include <string.h>

/* ---------- audio ---------- */
#define SAMPLE_RATE   44100
#define CHANNELS      2
#define BITS          16
#define I2S_BLOCK     1024                       /* 256 stereo frames = 5.8 ms */
#define I2S_BLOCKS    4
#define PCM_RING      (64 * 1024)                /* ~370 ms @ 176 kB/s */
#define PRIME_LEVEL   (PCM_RING / 2)

/* ---------- link ---------- */
#define SYNC0 0xA5
#define SYNC1 0x5A
#define HDR   8
#define MAX_PAYLOAD 512
#define T_AUDIO  1
#define T_CTRL   2
#define T_STATUS 3
#define FMT_EXPECTED 0x11                        /* 16-bit, 44.1k */
#define RX_DMA_BUF 1024
#define RX_RING   (16 * 1024)
#define STREAM_GAP_MS 500                        /* no AUDIO for this long = end of stream */

/* ---------- matrix ---------- */
#define MW 13
#define MH 8

static const struct device *const i2s_dev = DEVICE_DT_GET(DT_NODELABEL(sai2_a));
static const struct device *const uart    = DEVICE_DT_GET(DT_NODELABEL(lpuart1));
static const struct device *const matrix  = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
static uint8_t *fb;

K_MEM_SLAB_DEFINE_STATIC(i2s_slab, I2S_BLOCK, I2S_BLOCKS, 4);

static uint8_t pcm_store[PCM_RING];
static struct ring_buf pcm_ring;
static K_MUTEX_DEFINE(pcm_lock);

RING_BUF_DECLARE(rx_ring, RX_RING);
static K_SEM_DEFINE(rx_sem, 0, 1);
static uint8_t rx_dma[2][RX_DMA_BUF];
static uint8_t rx_next;

static volatile uint32_t st_bad, st_drop, st_underrun, st_rxovf, st_frames;
static uint64_t vu_sq_l, vu_sq_r;                /* sum of squares since last draw */
static uint32_t vu_n;                            /* frames in that sum */
static uint32_t st_consumed;                     /* PCM bytes fed to the SAI */
static uint32_t st_pcm_crc = 0xFFFFFFFFu;        /* running CRC-32 register */
static bool primed;
static uint16_t expect_seq;
static bool have_seq;
static int64_t last_audio;                       /* uptime of last accepted AUDIO frame */
static bool stream_active;

static uint8_t status_buf[HDR + 32 + 2];
static volatile bool tx_busy;

/* ---------- helpers ---------- */
/* CRC-32, zlib/PNG reflected polynomial 0xEDB88320, init ~0, final xor ~0.
 * Table is built once at boot; 1 KB of RAM buys ~8x over the bitwise loop. */
static uint32_t crc32_tab[256];
static void crc32_init(void)
{
	for (uint32_t i = 0; i < 256; i++) {
		uint32_t c = i;
		for (int k = 0; k < 8; k++) {
			c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
		}
		crc32_tab[i] = c;
	}
}
static uint32_t crc32_update(uint32_t crc, const void *buf, size_t n)
{
	const uint8_t *p = buf;
	while (n--) {
		crc = crc32_tab[(crc ^ *p++) & 0xFF] ^ (crc >> 8);
	}
	return crc;
}

static uint16_t crc16(const uint8_t *p, size_t n)
{
	uint16_t crc = 0xFFFF;
	while (n--) {
		crc ^= (uint16_t)(*p++) << 8;
		for (int i = 0; i < 8; i++) {
			crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
		}
	}
	return crc;
}

static inline void put_u16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
static inline void put_u32(uint8_t *p, uint32_t v)
{
	p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24;
}

static void bar(int row, uint32_t n)
{
	if (!fb) return;
	for (int i = 0; i < MW; i++) {
		fb[row * MW + i] = (i < (int)n) ? 7 : 0;
	}
}

/* ---------- VU meter ----------
 * RMS, not peak: a peak meter pins at full on real music (a loud master has
 * peaks at 0 dBFS with a ~10 dB crest factor, so a 50 ms peak is at the top of
 * any absolute scale essentially always -- measured at 12/13 pixels, 100 % of
 * the time).
 *
 * AUTO-GAIN, because volume on this board is digital and applied upstream of
 * the STM32 (librespot/MPD attenuate the samples; the PCM5102 has no analog
 * volume). An absolute scale therefore measures the listening volume, not the
 * music, and only reads full with the volume painfully high. Instead the bar is
 * shown relative to a reference that follows the recent loudest level: fast
 * attack, ~3 s exponential release. The display then fills at any volume and
 * still shows the music's dynamics.
 *
 * Bounds:
 *   MS_FLOOR  -60 dBFS absolute. Below this the bar is dark, so silence stays
 *             dark instead of the gain running away into the noise.
 *   REF_MIN   the reference cannot fall below -40 dBFS, which caps the gain at
 *             +40 dB.
 *
 * Everything is integer. Thresholds are ratios of the reference in Q16, and
 * comparisons are on MEAN SQUARE, so there is no square root and no float in
 * the loop that also sends STATUS.
 */
#define VU_FS_MS    1073741824u                  /* 32768^2 = 0 dBFS */
#define VU_MS_FLOOR 1074u                        /* -60 dBFS */
#define VU_REF_MIN  107374u                      /* -40 dBFS -> +40 dB max gain */
#define VU_RELEASE  60                           /* 1/60 per 50 ms draw = 3.0 s */

static const uint32_t vu_ratio_q16[MW] = {       /* -30..0 dB below the reference */
	111u, 190u, 323u, 549u, 934u, 1589u, 2703u, 4599u, 7824u, 13310u,
	22643u, 38522u, 65536u,
};
static uint32_t vu_ref = VU_REF_MIN;

static uint8_t vu_pixels(uint32_t mean_square, uint32_t ref)
{
	if (mean_square < VU_MS_FLOOR) {
		return 0;                        /* genuinely silent */
	}
	uint8_t n = 0;
	while (n < MW &&
	       (uint64_t)mean_square >= (((uint64_t)ref * vu_ratio_q16[n]) >> 16)) {
		n++;
	}
	return n;
}

/* One bar row: solid to n, plus the held peak as a detached pixel ahead of it. */
static void vu_row(int row, uint8_t n, uint8_t hold)
{
	for (int i = 0; i < MW; i++) {
		uint8_t v = 0;
		if (i < n) {
			v = 7;
		} else if (hold && i == hold - 1) {
			v = 7;
		}
		fb[row * MW + i] = v;
	}
}

/* Whole-matrix draw, ~20 fps from the main loop. Exactly one mode is shown. */
static uint8_t hold_l, hold_r;                   /* peak-hold position, pixels */
static int64_t hold_next;                        /* next 1-pixel decay */

static void draw_display(int64_t now)
{
	if (!fb) {
		return;
	}
	/* Every stream ends by taking a short tail block, which counts one
	 * underrun. At idle that single count is the normal end-of-track marker,
	 * not a fault -- treating it as one would replace the heartbeat with a
	 * permanent 1-pixel diagnostic after the first track ever played. */
	bool err = st_bad || st_drop || st_rxovf ||
		   (stream_active ? st_underrun : st_underrun > 1);

	if (err) {                               /* diagnostics outrank the VU */
		bar(0, st_bad);
		bar(1, st_drop);
		bar(2, st_underrun);
		bar(3, st_rxovf);
		bar(4, 0);
		bar(5, 0);
		bar(6, 0);
		k_mutex_lock(&pcm_lock, K_FOREVER);
		uint32_t fill = ring_buf_size_get(&pcm_ring);
		k_mutex_unlock(&pcm_lock);
		bar(7, (fill * MW + PCM_RING / 2) / PCM_RING);
		return;
	}

	if (!stream_active) {                    /* booted, linked, nothing playing */
		for (int r = 0; r < MH; r++) {
			bar(r, 0);
		}
		fb[3 * MW + 6] = ((now % 2000) < 150) ? 6 : 1;
		hold_l = hold_r = 0;
		vu_ref = VU_REF_MIN;             /* next stream starts from full gain */
		return;
	}

	k_mutex_lock(&pcm_lock, K_FOREVER);
	uint64_t sl = vu_sq_l, sr = vu_sq_r;
	uint32_t cnt = vu_n;
	vu_sq_l = 0;                             /* level is per draw interval */
	vu_sq_r = 0;
	vu_n = 0;
	k_mutex_unlock(&pcm_lock);

	uint8_t nl = 0, nr = 0;
	if (cnt) {
		uint32_t ml = (uint32_t)(sl / cnt), mr = (uint32_t)(sr / cnt);
		/* One reference for both channels, taken from the louder one, so the
		 * bars still show the stereo balance instead of each self-levelling. */
		uint32_t cur = ml > mr ? ml : mr;
		if (cur > vu_ref) {
			vu_ref = cur;            /* fast attack */
		} else if (vu_ref > VU_REF_MIN) {
			uint32_t d = vu_ref / VU_RELEASE;
			if (!d) {
				d = 1;           /* keep decaying once the quotient truncates */
			}
			vu_ref = (vu_ref - d < VU_REF_MIN) ? VU_REF_MIN : vu_ref - d;
		}
		nl = vu_pixels(ml, vu_ref);
		nr = vu_pixels(mr, vu_ref);
	}
	if (now >= hold_next) {
		hold_next = now + 300;
		if (hold_l) hold_l--;
		if (hold_r) hold_r--;
	}
	if (nl > hold_l) hold_l = nl;
	if (nr > hold_r) hold_r = nr;

	bar(0, 0);
	bar(3, 0);
	bar(4, 0);
	bar(7, 0);
	vu_row(1, nl, hold_l);
	vu_row(2, nl, hold_l);
	vu_row(5, nr, hold_r);
	vu_row(6, nr, hold_r);
}

/* ---------- UART async (DMA) ---------- */
static void uart_cb(const struct device *dev, struct uart_event *evt, void *ud)
{
	switch (evt->type) {
	case UART_RX_RDY: {
		uint32_t put = ring_buf_put(&rx_ring,
					    evt->data.rx.buf + evt->data.rx.offset,
					    evt->data.rx.len);
		if (put < evt->data.rx.len) {
			st_rxovf += evt->data.rx.len - put;
		}
		k_sem_give(&rx_sem);
		break;
	}
	case UART_RX_BUF_REQUEST:
		uart_rx_buf_rsp(dev, rx_dma[rx_next], RX_DMA_BUF);
		rx_next ^= 1;
		break;
	case UART_RX_DISABLED:
		/* Restart if the driver ever stops (overrun, error). */
		rx_next = 0;
		uart_rx_enable(dev, rx_dma[0], RX_DMA_BUF, 1000);
		rx_next = 1;
		break;
	case UART_TX_DONE:
	case UART_TX_ABORTED:
		tx_busy = false;
		break;
	default:
		break;
	}
}

static void send_status(void)
{
	if (tx_busy) {
		return;                          /* never block the parser */
	}
	uint8_t *p = status_buf;
	p[0] = SYNC0; p[1] = SYNC1;
	put_u16(p + 2, 0);
	p[4] = T_STATUS; p[5] = FMT_EXPECTED;
	put_u16(p + 6, 32);
	/* crc and consumed must come from the same instant as each other, or the
	 * sender would compare a CRC against a byte count one block apart. */
	k_mutex_lock(&pcm_lock, K_FOREVER);
	uint32_t fill = ring_buf_size_get(&pcm_ring);
	uint32_t pcm_crc = st_pcm_crc ^ 0xFFFFFFFFu;
	uint32_t consumed = st_consumed;
	k_mutex_unlock(&pcm_lock);
	put_u32(p + 8,  fill);
	put_u32(p + 12, PCM_RING);
	put_u32(p + 16, st_underrun);
	put_u32(p + 20, st_bad);
	put_u32(p + 24, st_drop);
	put_u32(p + 28, st_rxovf);
	put_u32(p + 32, pcm_crc);
	put_u32(p + 36, consumed);
	put_u16(p + 40, crc16(p + 2, 6 + 32));
	tx_busy = true;
	if (uart_tx(uart, status_buf, sizeof(status_buf), 10000)) {
		tx_busy = false;
	}
}

/* ---------- frame parser ---------- */
static uint8_t frame[HDR + MAX_PAYLOAD + 2];
static size_t fi, flen;

static void handle_frame(void)
{
	uint16_t seq = frame[2] | (frame[3] << 8);
	uint8_t type = frame[4], fmt = frame[5];
	uint16_t len = frame[6] | (frame[7] << 8);
	uint16_t crc_rx = frame[HDR + len] | (frame[HDR + len + 1] << 8);

	if (crc16(&frame[2], 6 + len) != crc_rx) {
		st_bad++;
		return;
	}
	if (have_seq && seq != expect_seq) {
		st_drop += (uint16_t)(seq - expect_seq);
	}
	expect_seq = seq + 1;
	have_seq = true;
	st_frames++;

	if (type == T_AUDIO) {
		if (fmt != FMT_EXPECTED) {
			st_bad++;
			return;
		}
		last_audio = k_uptime_get();
		k_mutex_lock(&pcm_lock, K_FOREVER);
		if (!stream_active) {
			/* First AUDIO frame of a new stream: clear the counters that
			 * describe the previous one. Under pcm_lock because the feeder
			 * also writes st_underrun.
			 */
			stream_active = true;
			st_underrun = 0;
			st_bad = 0;
			st_drop = 0;
			st_consumed = 0;
			st_pcm_crc = 0xFFFFFFFFu;
		}
		uint32_t put = ring_buf_put(&pcm_ring, &frame[HDR], len);
		k_mutex_unlock(&pcm_lock);
		if (put < len) {
			/* Sender overran us; drop the excess, STATUS will slow it. */
			st_drop++;
		}
	}
	/* T_CTRL: reserved for start/stop/set-rate (2A ignores it) */
}

static void feed(uint8_t b)
{
	if (fi == 0) { if (b == SYNC0) frame[fi++] = b; return; }
	if (fi == 1) {
		if (b == SYNC1) frame[fi++] = b;
		else fi = (b == SYNC0) ? 1 : 0;
		return;
	}
	frame[fi++] = b;
	if (fi == HDR) {
		flen = frame[6] | (frame[7] << 8);
		if (flen > MAX_PAYLOAD) {
			st_bad++;
			fi = 0;
		}
		return;
	}
	if (fi == HDR + flen + 2) {
		handle_frame();
		fi = 0;
	}
}

/* ---------- SAI feeder thread ---------- */
static void feeder(void *a, void *b, void *c)
{
	void *blk;

	while (1) {
		if (k_mem_slab_alloc(&i2s_slab, &blk, K_FOREVER)) {
			continue;
		}
		uint32_t got = 0;
		k_mutex_lock(&pcm_lock, K_FOREVER);
		uint32_t avail = ring_buf_size_get(&pcm_ring);
		if (!primed && avail >= PRIME_LEVEL) {
			primed = true;
		}
		if (primed && avail >= I2S_BLOCK) {
			got = ring_buf_get(&pcm_ring, blk, I2S_BLOCK);
		} else {
			if (primed) {
				/* Underrun. Take the short tail rather than stranding it: a
				 * sub-block remainder would otherwise sit in the ring forever
				 * and be glued onto the front of the next stream. It also lets
				 * consumed_bytes reach the sender's total exactly. */
				got = ring_buf_get(&pcm_ring, blk, avail);
				st_underrun++;
				primed = false;
			}
			memset((uint8_t *)blk + got, 0, I2S_BLOCK - got);
		}
		if (got) {
			st_consumed += got;
			st_pcm_crc = crc32_update(st_pcm_crc, blk, got);
			/* Same bytes, same thread: sum of squares per channel for the
			 * VU. ~512 samples per 5.8 ms block, well under a percent of
			 * this core. Each square fits a u32; the sum needs 64 bits. */
			const int16_t *sp = blk;
			uint32_t ns = got / 2;
			uint64_t sl = vu_sq_l, sr = vu_sq_r;
			for (uint32_t i = 0; i + 1 < ns; i += 2) {
				int32_t l = sp[i], r = sp[i + 1];
				sl += (uint32_t)(l * l);
				sr += (uint32_t)(r * r);
			}
			vu_sq_l = sl; vu_sq_r = sr; vu_n += ns / 2;
		}
		k_mutex_unlock(&pcm_lock);

		if (i2s_write(i2s_dev, blk, I2S_BLOCK)) {
			k_mem_slab_free(&i2s_slab, blk);
			k_sleep(K_MSEC(1));
		}
	}
}
K_THREAD_DEFINE(feeder_tid, 2048, feeder, NULL, NULL, NULL, 5, 0, K_TICKS_FOREVER);

/* ---------- main ---------- */
int main(void)
{
	crc32_init();

	if (device_is_ready(matrix)) {
		fb = display_get_framebuffer(matrix);
		display_blanking_off(matrix);
	}
	ring_buf_init(&pcm_ring, PCM_RING, pcm_store);

	/* SAI: start immediately with silence, never stop. */
	struct i2s_config cfg = {
		.word_size = BITS, .channels = CHANNELS,
		.format = I2S_FMT_DATA_FORMAT_I2S,
		.options = I2S_OPT_BIT_CLK_MASTER | I2S_OPT_FRAME_CLK_MASTER,
		.frame_clk_freq = SAMPLE_RATE,
		.mem_slab = &i2s_slab, .block_size = I2S_BLOCK, .timeout = 2000,
	};
	if (!device_is_ready(i2s_dev) || i2s_configure(i2s_dev, I2S_DIR_TX, &cfg)) {
		bar(0, 13); bar(1, 13);              /* two full rows = SAI failed */
		return 0;
	}
	for (int i = 0; i < 2; i++) {            /* pre-queue silence */
		void *blk;
		k_mem_slab_alloc(&i2s_slab, &blk, K_FOREVER);
		memset(blk, 0, I2S_BLOCK);
		i2s_write(i2s_dev, blk, I2S_BLOCK);
	}
	if (i2s_trigger(i2s_dev, I2S_DIR_TX, I2S_TRIGGER_START)) {
		bar(0, 13); bar(1, 13); bar(2, 13);  /* three rows = START failed */
		return 0;
	}
	k_thread_start(feeder_tid);

	/* UART: async RX via DMA. */
	if (!device_is_ready(uart) || uart_callback_set(uart, uart_cb, NULL)) {
		bar(3, 13); bar(4, 13);              /* rows 3-4 = uart failed */
		return 0;
	}
	rx_next = 1;
	uart_rx_enable(uart, rx_dma[0], RX_DMA_BUF, 1000);   /* 1 ms idle timeout */

	int64_t last = k_uptime_get(), last_draw = last;
	uint8_t chunk[256];

	while (1) {
		k_sem_take(&rx_sem, K_MSEC(20));

		uint32_t n;
		while ((n = ring_buf_get(&rx_ring, chunk, sizeof(chunk))) > 0) {
			for (uint32_t i = 0; i < n; i++) {
				feed(chunk[i]);
			}
		}

		int64_t now = k_uptime_get();

		/* End of stream: no AUDIO frame for STREAM_GAP_MS. Drop the sequence
		 * tracking so the next stream's first seq isn't counted as a gap, and
		 * un-prime so playback re-buffers instead of starting on a stale ring.
		 */
		if (stream_active && (now - last_audio) > STREAM_GAP_MS) {
			stream_active = false;
			have_seq = false;
			k_mutex_lock(&pcm_lock, K_FOREVER);
			primed = false;
			k_mutex_unlock(&pcm_lock);
		}

		if (now - last >= 100) {         /* STATUS cadence unchanged */
			last = now;
			send_status();
		}
		if (now - last_draw >= 50) {     /* ~20 fps */
			last_draw = now;
			draw_display(now);
		}
	}
	return 0;
}
