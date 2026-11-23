# vgm2mid_opn

Converts [VGM/VGZ](https://vgmrips.net/wiki/VGM_Specification) chip logs to
Standard MIDI Files. Decodes the Yamaha YM2203 (NEC PC-9801 FM sound boards) and
the first three FM channels of the YM2608 and YM2612.

```bash
python3 vgm2mid_opn.py "12 Mako's Theme.vgz"     # -> "12 Mako's Theme.mid"
python3 vgm2mid_opn.py *.vgz -o midi/            # batch into a directory
```

Output is format 1: one track per chip channel (`FM 1..3`, `SSG A..C`), plus a
percussion track on MIDI channel 10 when the mixer enables SSG noise.

## Requirements

Python 3.8 or newer. Standard library only.

## Options

| Option | |
| --- | --- |
| `-o, --out` | output file (one input) or directory (several) |
| `--loops N` | how many times to play the looped section (default 1) |
| `--stats` | per-channel summary of what was decoded |
| `--programs 4,38,4` | GM programs for FM 1..3, overriding the heuristic |
| `--noise-drum N` | force one GM drum note for SSG noise instead of choosing by noise period |
| `--no-drums` | drop SSG noise instead of mapping it to the drum channel |
| `--no-release` | end notes at key-off instead of holding through the carrier release |

## Decoding

**FM pitch** is derived from the 11-bit F-number and 3-bit block:

```
fn = FN * fM / (144 * 2^(20 - B))
```

`0xA4` holds the F-number's top three bits in bits 0-2 and the block in bits 3-5.
A check that notes land on semitones does not detect a constant factor error in
the formula, such as an octave offset, so the test suite pins the absolute value:
`fnum 1040, block 4 == 440 Hz`.

**SSG pitch** is derived from the 12-bit tone period. The SSG is clocked at half
the FM clock, from the chip's /6 prescaler:

```
f = (fM / 2) / (16 * TP)
```

**FM level** is the Total Level of the operators that are carriers under the
channel's algorithm (0-3: operator 4; 4: operators 3 and 4; 5 and 6: operators 2,
3 and 4; 7: all four). **SSG level** is the volume register, which also gates the
note: the SSG has no key-on bit.

**SSG noise** is decoded from the mixer register. A channel in noise mode does not
produce the tone-period pitch. The tone period is still latched while noise is
selected, because drivers write it before switching back and the chip sounds it on
the switch. Noise output becomes drum hits on MIDI channel 10 at a maximum rate of
one per 25 ms, with velocity taken from the volume register. The noise period
selects the drum: ≤10 crash cymbal 2 (note 57, "A2" in Logic's drum map), ≤18 open
hi-hat, ≤26 snare, otherwise low floor tom.

**Timing** is 120 BPM at 480 PPQ, so one tick is 1.04 ms and absolute times match
the log. A MIDI file's length equals the VGM's total sample count.

**Two-byte register writes** are coalesced. Drivers write 16-bit values one byte
at a time, a few samples apart. Applying the intermediate state produces
octave-scale pitch jumps and spurious retriggers, so only the last write of a
burst is applied; the earlier bytes are still latched.

**Note length** runs to key-off plus the carrier's release time from `SL`/`RR`,
capped at 400 ms and clamped so a note never overlaps the next note on the same
channel. `--no-release` ends notes at key-off instead.

## Compatibility

The npm `vgm2midi` package decodes SN76489, YM2612, YM2413, YM2151 and AY-8910. It
does not implement the YM2203; on a YM2203 log it writes a 14-byte `MThd` chunk
declaring zero tracks, which most players reject.

Paul Jensen's `vgm2mid` decodes the YM2203. It is a 32-bit VB6 application with a
GUI and no command line, requires the VB6 runtime, and requires Windows codepage
1252 for correct output timing.

## Verification

* **Pitch**: 1881 of 1882 note-ons (99.95%) across three PC-98 rips match the
  frequency implied by the registers at that instant, recomputed by a separate
  decoder written from the specification.
* **Absolute tuning**: a synthetic log with `fnum 1040, block 4` renders at
  440.3 Hz in VGMPlay.
* **Length**: 39.716 / 31.788 / 39.049 s rendered from the logs, against
  39.72 / 31.79 / 39.05 s of MIDI.
* **Structure**: every output parses with an independent SMF reader; all notes
  paired, no overlaps, `MTrk` and end-of-track correct in every track.

## Limitations

* Only FM channels 1-3 are decoded: all of a YM2203, the first half of a YM2608.
  ADPCM and DAC percussion are not decoded.
* Envelope generators are not modelled. Note boundaries and the release estimate
  described above are the only envelope handling; attacks and decays are not
  reproduced.
* The GM program per FM channel and the drum chosen for noise are heuristics,
  overridable with `--programs` and `--noise-drum`. Neither affects timing.
* SSG hardware envelopes (bit 4 of the volume register) are treated as full
  volume rather than modelled.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m pytest tests -q          # if pytest is installed
```

The tests generate their VGM files in-process from the specification. No game data
is required.

## Related tools

* [VGMPlay / libvgm](https://github.com/ValleyBell/libvgm) — reference player for
  VGM logs.
* [vgm2mid](https://vgmrips.net/wiki/Vgm2mid) — Paul Jensen's converter with
  Valley Bell's updates. Windows only.
* [jvgmtrans](https://github.com/dksrphm/jvgmtrans) — Java, cross-platform,
  decodes the YM2203.
* [VGMTrans](https://github.com/vgmtrans/vgmtrans) and
  [VGMToolbox](https://github.com/Manicsteiner/VGMToolbox) — convert proprietary
  sequence formats; neither reads VGM register logs.

## Credits

This decoder was written from specifications and rendered audio. No code from the
projects below is included.

* **The VGM specification** ([vgmrips.net](https://vgmrips.net/wiki/VGM_Specification))
  — header layout, the command table, data blocks, and the rule that every pointer
  in the header is relative to the field containing it. The parser follows this
  document.
* **Yamaha YM2203 Application Manual**, as quoted in the
  [`soundlog`](https://docs.rs/soundlog/latest/soundlog/chip/fnumber/struct.OpnSpec.html)
  crate's `OpnSpec` documentation — the F-number formula and its prescaler.
* **[ymfm](https://github.com/aaronsgiles/ymfm)** by Aaron Giles (BSD-3-Clause),
  read via 86Box's vendored copy — the relationship between the master clock and
  the SSG clock, which sets the SSG's tuning.
* **Paul Jensen** — author of the original `vgm2mid`.
* **Valley Bell** — maintainer of `vgm2mid` and of libvgm/VGMPlay. The
  chip-support notes in the `vgm2mid` update changelog list which chips are
  covered, and VGMPlay was the renderer used to verify this converter's output.

## Licence

MIT — see [LICENSE](LICENSE). This covers the source only. It does not cover VGM
files, game audio, or rendered audio of game music, which remain the property of
their respective rights holders.
