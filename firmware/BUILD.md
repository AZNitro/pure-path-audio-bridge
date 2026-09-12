# Firmware — build and flash

Zephyr application for the STM32U585AIIxQ on the Arduino UNO Q. It receives framed
PCM over LPUART1 at 3 Mbaud (DMA), buffers it in a ring, feeds SAI2 Block A as an I2S
master to a PCM5102A, and drives the 13×8 LED matrix.

## Build

Needs a Zephyr workspace (`west init` / `west update`) and the Zephyr SDK. Place this
`firmware/` directory anywhere and point `west build` at it:

```sh
west build -b arduino_uno_q/stm32u585xx /path/to/firmware
```

The board target is `arduino_uno_q/stm32u585xx` — **not** `arduino_uno_q/stm32u585xx/m33`,
which does not resolve.

Output lands in `build/zephyr/zephyr.elf` and `zephyr.bin` (~50 kB flash, ~119 kB RAM).

## Flash

There is no USB debug probe. SWD is bit-banged from the QRB2210's GPIO by OpenOCD
(`linuxgpiod`), wrapped by Arduino's `arduino-debug` helper, and reached over adb:

```sh
adb forward tcp:3333 tcp:3333
adb shell 'pkill -x openocd'          # kill strays, or arduino-debug will not bind
adb shell arduino-debug &
```

Then from the SDK gdb on the host:

```
target extended-remote :3333
load
compare-sections                       # every section must say "matched"
```

`west flash` and `west debug` do **not** work here — they spawn their own local OpenOCD
instead of using the forwarded port.

> ### Power-cycle after flashing. Never reset.
> The STM32U5 boot ROM traps `reset` and the board comes up in a state that needs the
> power removed anyway. Pull the USB-C, count to three, plug it back in.

## Notes that cost real time

1. **SAI parent vs child.** The `st,stm32-sai` compatible sits on the parent `&sai2`,
   not on `&sai2_a`. Enabling only the sub-block gives an undefined-reference link
   error. `clocks` also belongs on the parent.
2. **`CONFIG_HEAP_MEM_POOL_SIZE` must be non-zero.** `sai_sub_init()` calls
   `k_msgq_alloc_init()`, which uses the system heap; with the default 0 the device
   init quietly returns `-ENOMEM` into `device_state.init_res`. 32 bytes would do;
   this uses 16 kB because the display driver wants some too.
3. **44.1 kHz needs PLL2 with a fractional N.** HSI16 cannot get there (the ideal
   divider is 11.338 and the driver rounds to 11 — 3.07 % sharp). The overlay runs
   PLL2 from `clk_msis` at 4 MHz: N = 56 + 3670/8192, P = 20, giving PLL2P =
   11,289,599.61 Hz against an ideal 256 × 44,100 = 11,289,600 — an error of
   0.035 ppm. An integer N is arithmetically impossible at this VCO input, so FRACN
   is mandatory. `SAI2SEL` encoding is PLL2/PLL3/PLL1/PIN/HSI = 0/1/2/3/4.
4. **`arduino-router` holds `/dev/ttyHS1`.** Disable it or `bridge-tx` cannot open the
   link; the unit file declares `Conflicts=arduino-router.service` for this reason.
