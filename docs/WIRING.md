# Wiring — UNO Q ↔ PCM5102A module

Six wires. The STM32U585 is the I2S **master** (it generates BCK and LRCK from PLL2); the PCM5102A is a pure I2S slave and derives its own master clock from BCK with its internal PLL, so no MCLK wire is needed.

## Connections

| PCM5102A module pin | signal | direction | UNO Q header | STM32 pin / function |
|---|---|---|---|---|
| VIN | +5 V | power | **5V** | — |
| GND | ground | common | **GND** | — |
| BCK | bit clock, 64 × Fs = 2.8224 MHz | STM32 → DAC | **D13** | PB13 — `SAI2_SCK_A` |
| LCK | word clock (LRCK), 44.1 kHz | STM32 → DAC | **D19 / A5** | PC0 — `SAI2_FS_A` |
| DIN | serial data | STM32 → DAC | **D11** | PB15 — `SAI2_SD_A` |
| SCK | system clock in | — | **GND** | tie low → module uses internal PLL from BCK |

`[IMAGE: Fritzing diagram]`
`[IMAGE: photo of the actual wiring, labelled]`

## Module jumpers (GY-PCM5102 back side)

| pad | set to | meaning |
|---|---|---|
| FLT | L | normal-latency FIR filter |
| DEMP | L | de-emphasis off |
| **XSMT** | **H** | un-mute. Left low the module is silent — the most common "no sound" cause |
| FMT | L | I2S format (not left-justified) |

## Notes

- Keep the three signal wires under ~10 cm and run GND alongside them; BCK is 2.8 MHz.
- D11 and D13 are also the SPI MOSI/SCK pins and D19 is an I2C pin, so SPI and the second I2C bus are unavailable while the DAC is connected.
- Power the module from the 5 V header pin; it has its own regulator for the 3.3 V DAC supply.
- All UNO Q header pins belong to the STM32. The Qualcomm side has no direct header GPIO — audio reaches the STM32 over LPUART1 (`/dev/ttyHS1` on Linux), which is internal to the board and needs no wiring.
- Volume: the PCM5102A has no volume control. Set MPD and the Spotify client to 100 % and control loudness at the amplifier or headphone amp.

## Text schematic

```
        Arduino UNO Q (STM32U585 side)                GY-PCM5102A
        ┌──────────────────────────┐                ┌──────────────┐
        │  5V   ───────────────────┼────────────────┤ VIN          │
        │  GND  ───────────────────┼───────┬────────┤ GND          │
        │  D13  PB13 SAI2_SCK_A ───┼───────┼────────┤ BCK          │
        │  D19  PC0  SAI2_FS_A  ───┼───────┼────────┤ LCK          │
        │  D11  PB15 SAI2_SD_A  ───┼───────┼────────┤ DIN          │
        │                          │       └────────┤ SCK          │
        └──────────────────────────┘                │         ♪ ───┤ 3.5 mm out → amp
                                                    └──────────────┘
```
