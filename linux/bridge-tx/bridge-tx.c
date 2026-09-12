/*
 * bridge-tx — Phase 2B sender. Runs on the UNO Q (QRB2210), feeds the STM32.
 *
 *   gcc -O2 -o bridge-tx bridge-tx.c
 *   ./bridge-tx song.wav                       # 16-bit stereo WAV, 44.1k or 48k
 *   librespot --backend pipe ... | ./bridge-tx -   # raw s16le stereo on stdin
 *   ./bridge-tx /home/arduino/audio.fifo          # FIFO: many sources, one reader
 *
 * Options: -p /dev/ttyHS1  -b 3000000  -r 44100 (rate for stdin mode)  -q quiet
 *
 * Pacing: sends at the nominal PCM byte rate, corrected by a P-controller on
 * the STM32's ring fill (target 50 %). No stop/go, so the fill line stays flat.
 *
 * Frame: [A5 5A][seq u16][type u8][fmt u8][len u16][payload][crc16]   (see main.c)
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define PAYLOAD   512
#define HDR       8
#define T_AUDIO   1
#define T_STATUS  3
#define TARGET    0.50
#define KP        0.60     /* corr = KP * (TARGET - fill);  fill 0.40 -> +6 % */
#define CORR_MAX  0.25     /* never ask more than link headroom */
#define CORR_MIN -0.50
#define POLL_MS   200     /* FIFO idle poll; bounds status latency + SIGTERM */

static volatile int stop;
static void on_sig(int s) { (void)s; stop = 1; }

/* ---------- CRC-16/CCITT-FALSE, table driven ---------- */
static uint16_t crc_tab[256];
static void crc_init(void)
{
	for (int i = 0; i < 256; i++) {
		uint16_t c = i << 8;
		for (int j = 0; j < 8; j++) c = (c & 0x8000) ? (c << 1) ^ 0x1021 : c << 1;
		crc_tab[i] = c;
	}
}
static uint16_t crc16(const uint8_t *p, size_t n)
{
	uint16_t c = 0xFFFF;
	while (n--) c = (c << 8) ^ crc_tab[(c >> 8) ^ *p++];
	return c;
}

/* ---------- CRC-32 (zlib polynomial), must match the STM32 ---------- */
static uint32_t crc32_tab[256];
static void crc32_init(void)
{
	for (uint32_t i = 0; i < 256; i++) {
		uint32_t c = i;
		for (int k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
		crc32_tab[i] = c;
	}
}
static uint32_t crc32_update(uint32_t crc, const void *buf, size_t n)
{
	const uint8_t *p = buf;
	while (n--) crc = crc32_tab[(crc ^ *p++) & 0xFF] ^ (crc >> 8);
	return crc;
}

/* ---------- time ---------- */
static double now_s(void)
{
	struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec * 1e-9;
}

/* ---------- serial ---------- */
static speed_t baud_const(int b)
{
	switch (b) {
	case 1000000: return B1000000; case 2000000: return B2000000;
	case 3000000: return B3000000; case 4000000: return B4000000;
	default: return 0;
	}
}
static int open_serial(const char *path, int baud)
{
	int fd = open(path, O_RDWR | O_NOCTTY);
	if (fd < 0) { perror(path); return -1; }
	struct termios t;
	if (tcgetattr(fd, &t)) { perror("tcgetattr"); return -1; }
	cfmakeraw(&t);
	speed_t sp = baud_const(baud);
	if (!sp) { fprintf(stderr, "unsupported baud %d\n", baud); return -1; }
	cfsetispeed(&t, sp); cfsetospeed(&t, sp);
	t.c_cflag |= CLOCAL | CREAD | CRTSCTS;      /* hardware flow control */
	t.c_cc[VMIN] = 0; t.c_cc[VTIME] = 0;        /* non-blocking reads */
	if (tcsetattr(fd, TCSANOW, &t)) { perror("tcsetattr"); return -1; }
	tcflush(fd, TCIOFLUSH);
	return fd;
}
static int write_all(int fd, const uint8_t *p, size_t n)
{
	while (n) {
		ssize_t w = write(fd, p, n);            /* blocks on CTS, that's fine */
		if (w < 0) { if (errno == EINTR) continue; perror("write"); return -1; }
		p += w; n -= w;
	}
	return 0;
}

/* ---------- WAV ---------- */
static int wav_open(FILE *f, int *rate, int *ch, int *bits, long *data_len)
{
	uint8_t h[12];
	if (fread(h, 1, 12, f) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) return -1;
	for (;;) {
		uint8_t c[8];
		if (fread(c, 1, 8, f) != 8) return -1;
		uint32_t sz = c[4] | c[5] << 8 | c[6] << 16 | (uint32_t)c[7] << 24;
		if (!memcmp(c, "fmt ", 4)) {
			uint8_t fmt[16];
			if (sz < 16 || fread(fmt, 1, 16, f) != 16) return -1;
			*ch = fmt[2] | fmt[3] << 8;
			*rate = fmt[4] | fmt[5] << 8 | fmt[6] << 16 | fmt[7] << 24;
			*bits = fmt[14] | fmt[15] << 8;
			if (sz > 16) fseek(f, sz - 16, SEEK_CUR);
		} else if (!memcmp(c, "data", 4)) {
			*data_len = sz; return 0;
		} else {
			fseek(f, sz + (sz & 1), SEEK_CUR);
		}
	}
}

/* ---------- STATUS parser ---------- */
struct status { uint32_t fill, size, under, bad, drop, ovf;
                uint32_t crc32, consumed; int have_crc; int valid; };
static uint8_t rxbuf[8192]; static size_t rxlen;

static uint32_t u32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

static void poll_status(int fd, struct status *st)
{
	ssize_t n = read(fd, rxbuf + rxlen, sizeof(rxbuf) - rxlen);
	if (n > 0) rxlen += n;
	size_t i = 0;
	while (rxlen - i >= HDR + 2) {
		if (rxbuf[i] != 0xA5 || rxbuf[i + 1] != 0x5A) { i++; continue; }
		uint16_t len = rxbuf[i + 6] | rxbuf[i + 7] << 8;
		if (len > 512) { i += 2; continue; }
		if (rxlen - i < (size_t)(HDR + len + 2)) break;
		const uint8_t *fr = rxbuf + i;
		uint16_t crc = fr[HDR + len] | fr[HDR + len + 1] << 8;
		if (crc16(fr + 2, 6 + len) == crc && fr[4] == T_STATUS && len >= 24) {
			st->fill = u32(fr + 8);  st->size = u32(fr + 12); st->under = u32(fr + 16);
			st->bad  = u32(fr + 20); st->drop = u32(fr + 24); st->ovf  = u32(fr + 28);
			if (len >= 32) {                    /* Phase 5 firmware */
				st->crc32 = u32(fr + 32); st->consumed = u32(fr + 36);
				st->have_crc = 1;
			}
			st->valid = 1;
		}
		i += HDR + len + 2;
	}
	if (i) { memmove(rxbuf, rxbuf + i, rxlen - i); rxlen -= i; }
	if (rxlen == sizeof(rxbuf)) rxlen = 0;      /* garbage; resync */
}

/* ---------- status file for the web page ---------- */
#define STATUS_PATH "/run/bridge/status.json"
static void write_status(double t, uint64_t seq, double rate, double corr,
			 const struct status *st, int streaming)
{
	char tmp[] = STATUS_PATH ".tmp";
	FILE *o = fopen(tmp, "w");
	if (!o) return;
	fprintf(o, "{\"ts\":%.3f,\"streaming\":%d,\"frames\":%llu,\"rate_bps\":%.0f,"
		   "\"corr_pct\":%.2f,\"fill_pct\":%.1f,\"underrun\":%u,\"bad\":%u,"
		   "\"drop\":%u,\"rxovf\":%u,\"link\":%d}\n",
		t, streaming, (unsigned long long)seq, rate, corr * 100,
		st->valid && st->size ? 100.0 * st->fill / st->size : 0.0,
		st->under, st->bad, st->drop, st->ovf, st->valid);
	fclose(o);
	rename(tmp, STATUS_PATH);
}

/* ---------- main ---------- */
int main(int argc, char **argv)
{
	const char *port = "/dev/ttyHS1", *in = NULL;
	int baud = 3000000, rate = 44100, quiet = 0, opt;
	while ((opt = getopt(argc, argv, "p:b:r:q")) != -1) {
		if (opt == 'p') port = optarg; else if (opt == 'b') baud = atoi(optarg);
		else if (opt == 'r') rate = atoi(optarg); else if (opt == 'q') quiet = 1;
	}
	if (optind >= argc) { fprintf(stderr, "usage: bridge-tx [-p port] [-b baud] [-r rate] file.wav | -\n"); return 2; }
	in = argv[optind];

	/* Input: "-" = raw PCM on stdin; a FIFO = raw PCM, held open O_RDWR so it
	 * never hits EOF between sources; anything else = 16-bit stereo WAV. */
	FILE *f; int is_fifo = 0;
	if (!strcmp(in, "-")) {
		f = stdin;
	} else {
		struct stat sb;
		if (stat(in, &sb) == 0 && S_ISFIFO(sb.st_mode)) {
			int ifd = open(in, O_RDWR);
			if (ifd < 0 || !(f = fdopen(ifd, "rb"))) { perror(in); return 1; }
			fcntl(ifd, F_SETPIPE_SZ, 16384);     /* ~90 ms of audio, not 370 */
			/* No stdio read-ahead: poll() below must see exactly what
			 * fread() would consume, or buffered bytes could sit unsent. */
			setvbuf(f, NULL, _IONBF, 0);
			is_fifo = 1;
		} else {
			f = fopen(in, "rb");
			if (!f) { perror(in); return 1; }
			int ch = 0, bits = 0; long dlen = 0;
			if (wav_open(f, &rate, &ch, &bits, &dlen) || ch != 2 || bits != 16) {
				fprintf(stderr, "need 16-bit stereo WAV\n"); return 1;
			}
		}
	}
	uint8_t fmt;
	if (rate == 44100) fmt = 0x11; else if (rate == 48000) fmt = 0x12;
	else { fprintf(stderr, "rate must be 44100 or 48000\n"); return 1; }

	int fd = open_serial(port, baud);
	if (fd < 0) return 1;
	crc_init();
	crc32_init();
	signal(SIGINT, on_sig); signal(SIGTERM, on_sig);

	const double bps = rate * 4.0;                /* nominal PCM bytes/s */
	uint8_t frame[HDR + PAYLOAD + 2] = { 0xA5, 0x5A, 0, 0, T_AUDIO, fmt, PAYLOAD & 0xFF, PAYLOAD >> 8 };
	struct status st = { 0 };
	double t0 = now_s(), credit = 0, tlast = t0, tprint = t0, corr = CORR_MAX;
	uint64_t sent = 0, sent_prev = 0, seq = 0; int eof = 0;
	uint32_t sent_crc = 0xFFFFFFFFu;         /* over every payload byte written */

	if (!quiet) fprintf(stderr, "bridge-tx: %s @%d, %d Hz, nominal %.0f B/s\n", port, baud, rate, bps);

	while (!stop && !eof) {
		double t = now_s();
		credit += (t - tlast) * bps * (1.0 + corr);
		tlast = t;
		if (credit > 4 * PAYLOAD) credit = 4 * PAYLOAD;   /* no giant bursts */

		while (credit >= PAYLOAD && !eof) {
			/* A FIFO with no writer blocks fread() indefinitely, which used
			 * to stall the 1 Hz status write and swallow SIGTERM (SA_RESTART
			 * resumes the read). Poll first and fall through to the status
			 * tail while the sources are quiet. Pacing is unchanged: credit
			 * just stays clamped until audio comes back. */
			if (is_fifo) {
				struct pollfd pfd = { .fd = fileno(f), .events = POLLIN };
				if (poll(&pfd, 1, POLL_MS) <= 0) break;   /* idle, or EINTR */
			}
			size_t n = fread(frame + HDR, 1, PAYLOAD, f);
			if (n < PAYLOAD) {
				memset(frame + HDR + n, 0, PAYLOAD - n);
				if (!is_fifo) eof = 1;       /* a FIFO blocks instead of EOF */
			}
			frame[2] = seq & 0xFF; frame[3] = (seq >> 8) & 0xFF;
			uint16_t c = crc16(frame + 2, 6 + PAYLOAD);
			frame[HDR + PAYLOAD] = c & 0xFF; frame[HDR + PAYLOAD + 1] = c >> 8;
			if (write_all(fd, frame, sizeof(frame))) { stop = 1; break; }
			sent_crc = crc32_update(sent_crc, frame + HDR, PAYLOAD);
			seq++; sent += PAYLOAD; credit -= PAYLOAD;
		}

		poll_status(fd, &st);
		if (st.valid && st.size) {
			double fill = (double)st.fill / st.size;
			corr = KP * (TARGET - fill);
			if (corr > CORR_MAX) corr = CORR_MAX;
			if (corr < CORR_MIN) corr = CORR_MIN;
		}

		if (t - tprint >= 1.0) {
			double rate = (sent - sent_prev) / (t - tprint);   /* rolling 1 s */
			int streaming = sent > sent_prev;
			tprint = t; sent_prev = sent;
			write_status(t, seq, rate, corr, &st, streaming);
			if (!quiet) {
				fprintf(stderr, "[%6.1fs] sent=%llu fr  rate=%.0f B/s  corr=%+.1f%%  fill=%3.0f%%  "
					"underrun=%u bad=%u drop=%u rxovf=%u\n",
					t - t0, (unsigned long long)seq, rate, corr * 100,
					st.valid ? 100.0 * st.fill / st.size : -1.0,
					st.under, st.bad, st.drop, st.ovf);
			}
		}
		if (credit < PAYLOAD) { struct timespec ts = { 0, 1000000 }; nanosleep(&ts, NULL); }
	}

	/* let the STM32 drain what it holds, then leave (SAI keeps clocking silence) */
	tcdrain(fd);
	if (st.valid) { double tail = (double)st.fill / bps; struct timespec ts = { (time_t)tail, (long)((tail - (long)tail) * 1e9) }; nanosleep(&ts, NULL); }

	/* The drain estimate is built from a STATUS up to 100 ms old, so give the
	 * STM32 a moment to finish and settle: poll until consumed_bytes stops
	 * advancing for 400 ms, or 3 s have passed. Without this the comparison
	 * can read a byte count that is still a block or two short. */
	{
		uint32_t last_cons = st.consumed; double t_still = now_s(), t_end = t_still + 3.0;
		while (now_s() < t_end) {
			struct timespec ts = { 0, 20 * 1000 * 1000 };
			nanosleep(&ts, NULL);
			poll_status(fd, &st);
			if (st.consumed != last_cons) { last_cons = st.consumed; t_still = now_s(); }
			else if (now_s() - t_still > 0.4) break;
		}
	}

	{
		uint32_t mine = sent_crc ^ 0xFFFFFFFFu;
		int match = st.have_crc && st.consumed == (uint32_t)sent && st.crc32 == mine;
		printf("sent_bytes=%llu sent_crc32=%08x  stm32_consumed=%u stm32_crc32=%08x  %s\n",
		       (unsigned long long)sent, mine, st.consumed, st.crc32,
		       !st.have_crc ? "MISMATCH (old firmware: no crc in STATUS)"
		                    : (match ? "MATCH" : "MISMATCH"));
		fflush(stdout);
	}
	if (!quiet) fprintf(stderr, "done: %llu frames, underrun=%u bad=%u drop=%u\n",
			    (unsigned long long)seq, st.under, st.bad, st.drop);
	close(fd);
	return 0;
}
