# Operating notes, learnings and backlog

Field notes from running the bridge, kept separate from the build and install docs.
Everything here is observed behaviour, not design intent.

## Volume is digital, and the control point owns it

**The UPnP control point's volume slider drives MPD's software mixer. For bit-exact
playback keep it at 100 and use the amp; a slider at 0 gives silence and a dark VU.**

There is no analogue volume anywhere in this system. The PCM5102A has no gain stage, so
every volume control — MPD's software mixer, librespot's softvol, the slider in a UPnP
app or in the Spotify client — works by multiplying the samples before they reach the
bridge. Consequences worth remembering:

* A volume below 100 is still *correct* audio but no longer *bit-exact*, and the CRC
  check will not match a reference file.
* The control point writes its value into MPD, so it **overwrites whatever was set
  locally**. Check MPD's volume after casting, not before.
* At volume 0 the samples are all zero. Playback looks healthy at every level —
  the link is up, frames arrive, no underruns — and you hear nothing.
* The VU meter reads the samples fed to the SAI, so digital silence puts it below its
  −60 dBFS floor and the matrix goes dark. **A dark matrix during apparently healthy
  playback means the signal is silent, not that the display is broken.**

Observed 2026-09-12: UPnP playback reported as "broken", matrix dark. `mpc`-equivalent
status showed `volume: 0` while `link=1` and `underrun=bad=drop=rxovf=0`. Setting the
volume back to 100 was the whole fix.

## One source at a time, and it is not enforced

librespot, MPD and upmpdcli all write into the same `audio.fifo`, and nothing arbitrates
between them. Two sources playing at once interleave their blocks and the result
stutters.

Observed 2026-09-12 20:29: librespot was streaming a Spotify track while a UPnP cast was
also running. Stutter was reported for the UPnP stream. Disconnect Spotify before
casting, and vice versa.

## Unexplained power loss during a UPnP session

Observed 2026-09-12 20:31:14. The boot ended abruptly with no systemd shutdown sequence
and no kernel message of any kind in the preceding five minutes. Searched that boot for
`undervolt`, `brown`, `power supply`, `vbus`, `charger`, `overcurrent`, `thermal trip`,
`oom`, `watchdog`, `panic` and `BUG:` — nothing. Thermals measured 38–40 °C afterwards,
so thermal shutdown is unlikely.

Context at the time: librespot streaming (having just logged
`Throughput 2 kbps lower than minimum 8`), a UPnP cast of a remote HTTP MP3 in progress,
and Wi-Fi via a phone hotspot that was dropping DNS and websocket connections. Whether
that load caused a brownout, or the USB-C connector was disturbed, is not established.

**Recorded as unexplained.** If it recurs, the things to capture are the power source in
use (which supply, which cable, which port) and whether it correlates with concurrent
sources. Three reboots happened over the evening; only this one was unclean.

## Firmware backlog

### Flush the PCM ring on end-of-stream

The ring is not flushed when a stream ends — the firmware clears `primed` so playback
re-buffers, but leftover bytes stay in the ring. After an interrupted stream the idle
`fill_pct` therefore sits at whatever was left (observed: **31.2 %**, constant, with all
counters at zero) instead of returning to 0. It never drains, because the feeder only
resumes consuming once the ring reaches the 50 % prime level.

Consequence: the next stream opens with up to half a ring of stale audio from the
previous track glued to its front, and idle `fill_pct` is a misleading diagnostic.

**It also breaks the CRC proof.** Measured 2026-09-12 with 20,480 bytes stranded:

```
sent_bytes=5292032 sent_crc32=5c906e27  stm32_consumed=5312512 stm32_crc32=36ee26b8  MISMATCH
                                                    ^^^^^^^^^ 20,480 bytes more than were sent
```

20,480 / 65,536 = 31.2 %, exactly the stranded fill. The firmware consumed the stale
bytes ahead of the test file, so both the byte count and the CRC differ. Re-running with
the ring drained gave `5292032 / 5c906e27` on both sides — MATCH. So a MISMATCH whose
`stm32_consumed` *exceeds* `sent_bytes` is this bug, not a corrupted link; the shortfall
is the stale remainder, and a second run clears it.

Fix: flush the ring in the same place that clears `primed` at `STREAM_GAP_MS`. Deferred,
not flashed — the audio path is otherwise unaffected and all error counters stay at zero
— but it should be the first firmware change after the freeze, because it can make a
clean system look like it failed its own correctness test.
