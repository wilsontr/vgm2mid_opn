# vgm2mid_opn

Converts [VGM/VGZ](https://vgmrips.net/wiki/VGM_Specification) sound chip logs to Standard MIDI Files (.mid).

The tool decodes:
* The Yamaha YM2203 (used on NEC PC-9801 sound boards)
* The first three FM channels of the Yamaha YM2608 and YM2612

```bash
python3 vgm2mid_opn.py "12 Mako's Theme.vgz"     # -> "12 Mako's Theme.mid"
python3 vgm2mid_opn.py *.vgz -o midi/            # batch process into a directory
```

The output file is a Format 1 MIDI file:
* Each chip channel receives its own track (`FM 1..3`, `SSG A..C`).
* When the chip mixer enables SSG noise, an extra percussion track is written to MIDI channel 10.

## Requirements

* Python 3.8 or newer
* Python standard library only (no external dependencies)

## Options

| Option | Description |
| --- | --- |
| `-o, --out` | Output file path (single input) or output directory (multiple inputs) |
| `--loops N` | Number of times to play looped sections (default: 1) |
| `--stats` | Print a per-channel summary of decoded events |
| `--programs 4,38,4` | General MIDI program numbers for FM channels 1 to 3, overriding the default heuristic |
| `--noise-drum N` | Map all SSG noise to a fixed General MIDI drum note number instead of selecting by noise period |
| `--no-drums` | Drop SSG noise entirely instead of writing drum notes |
| `--no-release` | Cut notes off immediately at key-off instead of extending them through the carrier release phase |

## Decoding

### FM Pitch

FM note frequency is calculated from the 11-bit F-number (`FN`), the 3-bit octave block (`B`), and the chip master clock (`fM`):

```
fn = FN * fM / (144 * 2^(20 - B))
```

In the YM2203 register set, register `0xA4` holds the top three bits of the F-number in bits 0–2, and the octave block in bits 3–5.

Checking that notes land on semitones does not catch constant scaling errors, such as an octave offset. To prevent this, the test suite verifies an absolute reference value: `fnum 1040, block 4 == 440 Hz`.

### SSG Pitch

SSG pitch is derived from the 12-bit tone period (`TP`). An internal /6 prescaler runs the SSG section at half the master FM clock rate (`fM / 2`):

```
f = (fM / 2) / (16 * TP)
```

### Volume and Velocity

* **FM Channels**: Velocity is calculated from the Total Level (TL) settings of the operators acting as carriers. Which operators are carriers depends on the channel's algorithm:
  * Algorithms 0–3: Operator 4
  * Algorithm 4: Operators 3 and 4
  * Algorithms 5 and 6: Operators 2, 3, and 4
  * Algorithm 7: All four operators
* **SSG Channels**: Velocity is read directly from each channel's volume register. Because the SSG has no key-on bit, changes in this volume register also gate note-on and note-off events.

### SSG Noise

SSG noise state is read from the mixer register. When a channel is switched to noise mode, tone generation stops for that channel.

The channel's tone period register continues to latch writes made while noise mode is active. Sound drivers frequently update the tone period register right before switching back to tone mode, and the chip outputs the latched pitch as soon as the switch occurs.

Noise events are converted into MIDI drum notes on channel 10:
* Note velocity is read from the channel volume register.
* Events are rate-limited to a maximum of one drum hit per 25 ms.
* The drum note is chosen based on the noise period:
  * 0–10: Crash Cymbal 2 (MIDI note 57, labeled "A2" in Logic's drum map)
  * 11–18: Open Hi-Hat (MIDI note 46)
  * 19–26: Snare (MIDI note 38)
  * 27+: Low Floor Tom (MIDI note 41)

### Timing

MIDI files are written at 120 BPM with a resolution of 480 pulses per quarter note (PPQ).

Under these settings, one MIDI tick is approximately 1.04 ms. Timestamps match the sample clock of the source log, and the total duration of the MIDI file matches the total sample count in the VGM file.

### Multi-Byte Register Writes

Sound drivers write 16-bit values as two single-byte writes separated by a few audio samples.

Applying intermediate values immediately causes octave-wide pitch jumps and false note retriggers. To avoid this, the decoder coalesces rapid writes: earlier bytes update internal chip state (latched), but note and pitch changes are applied only on the final write of a burst.

### Note Duration

A note begins at key-on and extends past key-off through the carrier operator's release phase.

The release duration is calculated from the Sustain Level (`SL`) and Release Rate (`RR`) registers. This release tail is capped at 400 ms and clamped so it never overlaps the next note on the same channel. The `--no-release` flag disables this tail, cutting notes off immediately at key-off.

## Compatibility

* **npm `vgm2midi`**: Decodes SN76489, YM2612, YM2413, YM2151, and AY-8910. It does not support the YM2203. When processing a YM2203 log, it generates an empty 14-byte `MThd` header with zero tracks, which most MIDI players reject.
* **Paul Jensen's `vgm2mid`**: Decodes the YM2203. It is a 32-bit Windows GUI program written in Visual Basic 6 with no command-line interface. It requires the VB6 runtime and requires Windows code page 1252 to produce correct timing.

## Verification

* **Pitch accuracy**: Across three PC-98 rips, 1,881 of 1,882 note-on events (99.95%) match the frequency calculated by an independent reference decoder written directly from the hardware specification.
* **Absolute tuning**: A synthetic test log using `fnum 1040, block 4` renders at 440.3 Hz in VGMPlay.
* **Duration**: Rendered audio lengths from source logs match the generated MIDI file lengths:
  * Log 1: 39.716 s audio vs. 39.72 s MIDI
  * Log 2: 31.788 s audio vs. 31.79 s MIDI
  * Log 3: 39.049 s audio vs. 39.05 s MIDI
* **File structure**: All outputs validate under an independent Standard MIDI File parser. Note-on and note-off events are properly paired without overlaps, and `MTrk` chunks and end-of-track markers are structurally correct across all tracks.

## Limitations

* **Channels**: Only FM channels 1 to 3 are decoded. This covers the entire FM section of the YM2203 and the first half of the YM2608. ADPCM audio and DAC percussion are not decoded.
* **Envelopes**: Envelope generators are not fully modeled. Attack and decay curves are omitted; the decoder only tracks note boundaries and carrier release tails.
* **Instrument assignment**: General MIDI program numbers for FM channels and drum note selections for noise are heuristics. These can be overridden using `--programs` and `--noise-drum`. Overriding them does not affect timing.
* **SSG hardware envelopes**: When bit 4 of an SSG volume register is set (enabling hardware envelope mode), the decoder treats the channel as full volume rather than modeling the envelope pattern.

## Tests

Run the test suite with Python's built-in `unittest` module:

```bash
python3 -m unittest discover -s tests -v
```

If `pytest` is installed, it can also be run directly:

```bash
python3 -m pytest tests -q
```

The test suite generates synthetic VGM data streams in memory from the specification. No external game files or audio dumps are required.

## Related Tools

* [VGMPlay / libvgm](https://github.com/ValleyBell/libvgm) — Reference player for VGM logs.
* [vgm2mid](https://vgmrips.net/wiki/Vgm2mid) — Paul Jensen's converter with updates by Valley Bell (Windows only).
* [jvgmtrans](https://github.com/dksrphm/jvgmtrans) — Cross-platform Java converter that decodes the YM2203.
* [VGMTrans](https://github.com/vgmtrans/vgmtrans) and [VGMToolbox](https://github.com/Manicsteiner/VGMToolbox) — Converters for proprietary game sequence formats. Neither tool reads VGM register logs.

## Credits

This decoder was written from hardware specifications and verified against rendered audio. It does not include code from the projects listed below.

* **[VGM Specification](https://vgmrips.net/wiki/VGM_Specification)** — Header structure, command tables, data block formats, and the specification rule that header pointers are relative to their field offset.
* **Yamaha YM2203 Application Manual** (quoted in the [`soundlog`](https://docs.rs/soundlog/latest/soundlog/chip/fnumber/struct.OpnSpec.html) crate documentation) — F-number formula and prescaler ratios.
* **[ymfm](https://github.com/aaronsgiles/ymfm)** by Aaron Giles (BSD-3-Clause, referenced via 86Box) — Clock relationship between the master clock and the SSG clock.
* **Paul Jensen** — Author of the original `vgm2mid`.
* **Valley Bell** — Maintainer of `vgm2mid` and `libvgm`/`VGMPlay`. The `vgm2mid` changelog provided chip support references, and VGMPlay served as the reference audio renderer for validation.

## License

MIT — see [LICENSE](LICENSE).

This license covers the source code only. It does not cover VGM files, game audio data, or rendered audio recordings of game music, which remain the property of their respective copyright holders.
