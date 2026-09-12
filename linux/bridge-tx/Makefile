# bridge-tx: FIFO/WAV -> framed PCM on /dev/ttyHS1, with the pure-path CRC check.
CFLAGS ?= -O2 -Wall -Wextra
bridge-tx: bridge-tx.c
	$(CC) $(CFLAGS) -o $@ $<
clean:
	rm -f bridge-tx
.PHONY: clean
