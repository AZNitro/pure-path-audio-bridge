# Tools

Test and calibration helpers. All of them run **on the board**, not on a host, and
default to a deployment under `/home/arduino` — override with `BRIDGE_HOME`.

| script | what it does |
|---|---|
| `purecrc.sh` | the bit-exactness check: one WAV through `bridge-tx`, prints MATCH / MISMATCH |
| `longcrc.sh` | the same check unattended over a long file; verdict lands in `longcrc.result` |
| `feed440.py` | paces `test440.raw` into `audio.fifo` at real time, for switching tests |
| `clicktest.sh` | captures `bridge.fifo` across mode and preset changes so slew can be measured |
| `vucal.py` | VU sensitivity calibration — applies only to the fixed-scale firmware revision |
| `wav_send.py` | the original Python sender, superseded by `bridge-tx`; kept for reference |
| `bw_test.py` | raw link bandwidth probe |
| `spotify-event.sh` | not a test: the `librespot --onevent` hook that publishes now-playing |

`spotify-event.sh` is deployed to `/home/arduino/` and referenced by
`librespot.service`; it lives here because it is a small standalone script rather
than part of any component.

## Reading a CRC result

`bridge-tx` prints one line at the end:

```
sent_bytes=<n> sent_crc32=<a>  stm32_consumed=<m> stm32_crc32=<b>  MATCH|MISMATCH
```

Both the counts and the CRCs must agree. `MISMATCH (old firmware: no crc in STATUS)`
means the firmware predates the 32-byte STATUS payload and cannot report a CRC at all.

If `stm32_consumed` is **larger** than `sent_bytes`, the difference is stale audio left
in the PCM ring by an interrupted stream — see `docs/OPERATING-NOTES.md`. Run the check
again; the first run drains the ring and the second matches. Check `fill_pct` in
`/run/bridge/status.json` reads 0.0 before trusting a CRC result.
