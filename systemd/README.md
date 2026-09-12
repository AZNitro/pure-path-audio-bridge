# Services

Six services make up a running bridge. Four are ours; MPD and upmpdcli run their stock
distribution units with the configuration in this directory.

| unit | what it does |
|---|---|
| `bridge-tx.service` | reads `bridge.fifo`, frames and paces it onto `/dev/ttyHS1` |
| `enhance.service` | reads `audio.fifo`, applies EQ (or passes bytes through), writes `bridge.fifo` |
| `bridge-api.service` | HTTP control API and web UI on `:8080` |
| `librespot.service` | Spotify Connect receiver writing into `audio.fifo` |
| *(stock)* `mpd.service` | local playback; see `mpd.conf` |
| *(stock)* `upmpdcli.service` | UPnP/DLNA renderer; see `upmpdcli.conf` |

## Install

Paths assume the deployment user is `arduino` with a home at `/home/arduino`. Change
`User=` and the paths in the units if yours differs.

```sh
# our units
sudo cp bridge-tx.service bridge-api.service enhance.service librespot.service \
        /etc/systemd/system/
# config for the stock services
sudo cp mpd.conf /etc/mpd.conf
sudo cp upmpdcli.conf /etc/upmpdcli.conf

# arduino-router owns /dev/ttyHS1 and must not run
sudo systemctl disable --now arduino-router.service

sudo systemctl daemon-reload
sudo systemctl enable --now bridge-tx enhance bridge-api librespot mpd upmpdcli
```

`bridge-tx` creates `/run/bridge` via `RuntimeDirectory=` and keeps it across restarts
via `RuntimeDirectoryPreserve=yes`, so mode and preset survive a service restart (a
reboot still clears them — that is what `/var/lib/bridge` is for). `enhance` and
`bridge-api` get `/var/lib/bridge` via `StateDirectory=`, where the custom EQ and the
per-context tweaks persist.

## One source at a time

librespot, MPD and upmpdcli all write into the same `audio.fifo`. Nothing arbitrates
between them: if two play at once their blocks interleave and the result stutters.
Disconnect Spotify before casting over UPnP, and vice versa.

## Volume must be at unity for bit-exactness

Both MPD and librespot attenuate in the sample domain. `mpd.conf` uses a software mixer
and `librespot.service` starts at `--initial-volume 100`; anything less is still correct
audio but no longer bit-exact, and the CRC check will not match a reference file. Use
the amplifier for loudness. A Spotify client's own volume slider drives librespot's
softvol, so leave it at maximum when you want the guarantee to hold.

The UPnP control point's volume slider drives MPD's software mixer. For bit-exact
playback keep it at 100 and use the amp; a slider at 0 gives silence and a dark VU.
Because the control point writes that value straight into MPD, it also overwrites any
volume set locally — check MPD's volume after casting, not before.
