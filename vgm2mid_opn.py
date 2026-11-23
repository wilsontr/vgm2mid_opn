#!/usr/bin/env python3
"""
vgm2mid_opn -- convert VGM/VGZ chip logs to Standard MIDI Files.

Targets the Yamaha OPN family, which is what NEC PC-98 FM boards use:

    YM2203 (OPN)   3 FM channels + AY-3-8910 style SSG
    YM2608 (OPNA)  6 FM channels + SSG + ADPCM

Most VGM-to-MIDI tools skip the YM2203, so a PC-98 log comes out empty or
wrong.  This one decodes it: FM pitch from the F-number/block, SSG pitch from
the tone period, FM level from the carrier operators, and SSG noise mapped to
a GM drum kit.

Usage:
    ./vgm2mid_opn.py song.vgz                 # -> song.mid
    ./vgm2mid_opn.py *.vgz -o midi/           # batch into a directory
    ./vgm2mid_opn.py song.vgm --loops 2       # play the loop twice
    ./vgm2mid_opn.py song.vgm --stats         # report what was decoded

Requires Python 3.8+.  No third-party packages.
"""

from __future__ import annotations

import argparse
import gzip
import math
import os
import struct
import sys

SAMPLE_RATE = 44100
PPQ = 480
TEMPO_US = 500_000                       # 120 BPM
TICKS_PER_SEC = PPQ * 1_000_000.0 / TEMPO_US
BEND_RANGE_SEMITONES = 2.0
BEND_HALF = 8192

# GM programs used by the heuristic
PROG_SQUARE = 80                         # Lead 1 (square) -- SSG is a square wave
PROG_SAW = 81
PROG_BASS = 33
PROG_EPIANO = 4

# OPN algorithm -> carrier operator indices (0..3 = op1..op4)
CARRIERS = {
    0: (3,), 1: (3,), 2: (3,), 3: (3,),
    4: (2, 3), 5: (1, 2, 3), 6: (1, 2, 3), 7: (0, 1, 2, 3),
}


def freq_to_note(freq: float) -> float:
    """Fractional MIDI note number for a frequency in Hz."""
    return 69.0 + 12.0 * math.log2(freq / 440.0)


def db_to_velocity(db: float) -> int:
    """Map 0..-48 dB of attenuation onto MIDI 1..127, gently compressed."""
    if db > 60.0:
        return 1
    return max(1, min(127, int(round(127.0 * max(0.0, 1.0 - db / 48.0) ** 0.7))))


# ---------------------------------------------------------------------------
# VGM container
# ---------------------------------------------------------------------------

class Vgm:
    def __init__(self, path):
        with open(path, "rb") as fp:
            raw = fp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        if raw[:4] != b"Vgm ":
            raise ValueError("%s: not a VGM file" % path)
        self.path = path
        self.raw = raw
        self.version = struct.unpack_from("<I", raw, 0x08)[0]
        self.total_samples = struct.unpack_from("<I", raw, 0x18)[0]

        # clock fields, with the dual-chip bit masked off
        self.clocks = {
            "YM2612": struct.unpack_from("<I", raw, 0x2C)[0] & 0x3FFFFFFF,
            "YM2203": struct.unpack_from("<I", raw, 0x44)[0] & 0x3FFFFFFF,
            "YM2608": struct.unpack_from("<I", raw, 0x48)[0] & 0x3FFFFFFF,
        }

        # every pointer in the header is relative to the field holding it
        data_rel = struct.unpack_from("<I", raw, 0x34)[0]
        self.data_start = (0x34 + data_rel) if data_rel else 0x40
        loop_rel = struct.unpack_from("<I", raw, 0x1C)[0]
        self.loop_offset = (0x1C + loop_rel) if loop_rel else 0
        if self.loop_offset >= len(raw):
            self.loop_offset = 0

        self.gd3 = self._read_gd3()

    def _read_gd3(self):
        rel = struct.unpack_from("<I", self.raw, 0x14)[0]
        if not rel:
            return {}
        off = 0x14 + rel
        if self.raw[off:off + 4] != b"Gd3 ":
            return {}
        length = struct.unpack_from("<I", self.raw, off + 8)[0]
        text = self.raw[off + 12:off + 12 + length].decode("utf-16-le", "replace")
        # GD3 order is English first, then the Japanese original
        keys = ["track_en", "track_jp", "game_en", "game_jp",
                "system_en", "system_jp", "author_en", "author_jp",
                "date", "ripper", "notes"]
        fields = text.split("\x00")
        return {k: v for k, v in zip(keys, fields) if v}

    def chip(self):
        for name in ("YM2203", "YM2608", "YM2612"):
            if self.clocks[name]:
                return name
        return None

    def stream(self):
        """Yield (sample_time, command, register, value).

        command is an int for chip writes, or the strings 'loop' and 'end'.
        """
        raw = self.raw
        p = self.data_start
        end = struct.unpack_from("<I", raw, 0x04)[0]
        end = min(end if end else len(raw) - 1, len(raw) - 1)
        t = 0
        seen_loop = self.loop_offset == 0
        while p < end:
            if not seen_loop and p >= self.loop_offset:
                yield (t, "loop", 0, 0)
                seen_loop = True
            cmd = raw[p]
            if cmd in (0x52, 0x53, 0x55, 0x56, 0x57, 0x58):
                yield (t, cmd, raw[p + 1], raw[p + 2])
                p += 3
            elif cmd == 0x61:
                t += raw[p + 1] | (raw[p + 2] << 8)
                p += 3
            elif cmd == 0x62:
                t += 735
                p += 1
            elif cmd == 0x63:
                t += 882
                p += 1
            elif 0x70 <= cmd <= 0x7F:
                t += (cmd & 0x0F) + 1
                p += 1
            elif cmd == 0x66:
                break
            elif cmd == 0x67:                    # data block
                p += 7 + struct.unpack_from("<I", raw, p + 3)[0]
            elif cmd == 0x68:                    # PCM RAM write
                p += 12
            elif cmd in (0x50, 0x30):            # SN76489
                p += 2
            elif cmd in (0x51, 0x54, 0x5A, 0x5B, 0x5C, 0x5D):   # OPL-ish, YM2413, YM2151
                p += 3
            elif 0x90 <= cmd <= 0x95:            # DAC stream control
                p += {0x90: 5, 0x91: 5, 0x92: 6, 0x93: 11, 0x94: 2, 0x95: 5}[cmd]
            elif 0x31 <= cmd <= 0x3F:
                p += 2
            elif 0x40 <= cmd <= 0x4E:
                p += 3
            else:
                p += 1
        yield (t, "end", 0, 0)


# ---------------------------------------------------------------------------
# note extraction
# ---------------------------------------------------------------------------

class Channel:
    """One chip channel -> one MIDI track."""

    def __init__(self, name, program):
        self.name = name
        self.program = program
        self.events = []            # (sample, action, a, b)
        self.use_release = True
        self.note = None
        self.velocity = 100
        self.bend = BEND_HALF
        self.note_start = -1e9

    def note_on(self, t, freq, velocity):
        note_f = freq_to_note(freq)
        note = max(0, min(127, int(round(note_f))))
        if self.note is not None:
            # retrigger: close the old note first, or it hangs forever
            self.note_off(t)
        self.note = note
        self.note_start = t
        self.velocity = velocity
        self.events.append((t, "on", note, velocity))
        self.bend = -1                            # force a bend message
        self._bend(t, note_f - note)

    def note_off(self, t, release_ms=0.0):
        if self.note is None:
            return
        self.events.append((t, "off", self.note, release_ms))
        self.note = None

    def _bend(self, t, semitones):
        raw = BEND_HALF + int(round(semitones / BEND_RANGE_SEMITONES * BEND_HALF))
        raw = max(0, min(16383, raw))
        if raw != self.bend:
            self.bend = raw
            self.events.append((t, "bend", raw, 0))

    def retune(self, t, freq):
        """Follow a pitch change on a sounding note; retrigger if too far."""
        if self.note is None:
            return
        note_f = freq_to_note(freq)
        if abs(note_f - self.note) > BEND_RANGE_SEMITONES:
            if t - self.note_start < 0.005 * SAMPLE_RATE:
                return              # just started: don't clip it into a blip
            self.note_on(t, freq, self.velocity)
        else:
            self._bend(t, note_f - self.note)

    def level(self, t, velocity):
        if self.note is not None and velocity != self.velocity:
            self.velocity = velocity
            self.events.append((t, "expr", velocity, 0))


class FmChannel(Channel):
    def __init__(self, index, name, clock, program=PROG_EPIANO):
        super().__init__(name, program)
        self.index = index
        self.clock = clock
        self.regs = [0] * 0x100
        self.keyed = False

    def carriers(self):
        return CARRIERS.get((self.regs[0xB0 + self.index] >> 3) & 7, (3,))

    def freq(self):
        # 0xA4 holds an 11-bit F-number in bits 0-2 and the 3-bit block in
        # bits 3-5, per the YM2203 application manual:
        #     fn = FN * fM / (144 * 2^(20 - B))
        low = self.regs[0xA0 + self.index] & 0xFF
        high = self.regs[0xA4 + self.index] & 0xFF
        fnum = ((high & 0x07) << 8) | low
        block = (high >> 3) & 0x07
        if fnum == 0:
            return 0.0
        return fnum * self.clock / (144.0 * 2.0 ** (20 - block))

    def loudness(self):
        db = min(0.75 * (self.regs[0x40 + op * 4 + self.index] & 0x3F)
                 for op in self.carriers())
        return db_to_velocity(db)

    def release_ms(self):
        """How long the carrier keeps ringing after key-off (from SL/RR).

        Key-off is instant in the log but not on the chip: without this the
        MIDI clips every note dead and a continuous bass line turns into
        staccato with gaps.
        """
        op = self.carriers()[0]
        v = self.regs[0x80 + op * 4 + self.index]
        rr = v & 0x0F
        sl = (v >> 4) & 0x0F
        if rr == 0:
            ms = 350.0                      # no release rate: effectively held
        else:
            ms = 8.0 * (2.0 ** ((15 - rr) * 0.5))
        if sl == 0:
            ms *= 0.25                      # already decayed to nothing
        return min(ms, 400.0)

    def write(self, t, reg, val, settled=True):
        self.regs[reg] = val
        if not settled:
            return
        if reg == 0x28:
            self._key(t, val)
        elif 0xA0 <= reg <= 0xA6 and self.note is not None:
            f = self.freq()
            if f > 8.0:
                self.retune(t, f)
        elif 0x40 <= reg <= 0x9E and self.note is not None:
            self.level(t, self.loudness())

    def _key(self, t, val):
        if (val & 0x07) != self.index:
            return
        on = bool(val & 0xF0)
        if on and not self.keyed:
            f = self.freq()
            if f > 8.0:
                self.note_on(t, f, self.loudness())
        elif not on and self.keyed:
            self.note_off(t, self.release_ms() if self.use_release else 0.0)
        self.keyed = on


class SsgChannel(Channel):
    """AY-3-8910 style square channel.  No key-on: volume gates the note."""

    def __init__(self, index, name, clock, drums=None, drum_override=None):
        super().__init__(name, PROG_SQUARE)
        self.index = index
        self.clock = clock
        self.period = 0
        self.volume = 0
        self.noise_period = 0
        self.noise_on = False
        self.drums = drums       # Percussion sink for noise output
        self.drum_override = drum_override

    def freq(self):
        if self.period < 2:
            return 0.0
        return self.clock / (16.0 * self.period)

    def drum_note(self):
        """Pick a GM drum from the noise period (short period = bright hiss)."""
        if self.drum_override is not None:
            return self.drum_override
        p = self.noise_period
        if p <= 10:
            return 57                   # crash cymbal 2 ("A2" in Logic's map)
        if p <= 18:
            return 46                   # open hi-hat
        if p <= 26:
            return 38                   # snare
        return 41                       # low floor tom

    def write(self, t, reg, val, settled=True):
        if reg == 0x06:
            self.noise_period = val & 0x1F
            return
        if reg == 0x07:
            # bit 3+n high = noise disabled for that channel
            self.noise_on = not (val & (0x08 << self.index))
            if self.noise_on:
                self.note_off(t)        # tone register is inaudible from here
            return
        if reg <= 0x05:
            if (reg >> 1) != self.index:
                return
            if reg & 1:
                self.period = (self.period & 0xFF) | ((val & 0x0F) << 8)
            else:
                self.period = (self.period & 0xF00) | val
            if self.noise_on:
                return                  # latched, but inaudible in noise mode
            if settled and self.note is not None:
                f = self.freq()
                if f > 8.0:
                    self.retune(t, f)
        elif 0x08 <= reg <= 0x0A:
            if (reg - 0x08) != self.index:
                return
            # bit 4 is the hardware envelope enable; treat it as full volume
            self.volume = 15 if val & 0x10 else (val & 0x0F)
            if not settled:
                return
            if self.noise_on:
                if self.drums and self.volume:
                    vel = max(1, min(127, int(round(self.volume / 15.0 * 127))))
                    self.drums.hit(t, self.drum_note(), vel)
                return
            self._gate(t)

    def _gate(self, t):
        vel = max(1, min(127, int(self.volume / 15.0 * 127)))
        if self.volume and self.note is None:
            f = self.freq()
            if f > 8.0:
                self.note_on(t, f, vel)
        elif not self.volume and self.note is not None:
            self.note_off(t)
        elif self.volume:
            self.level(t, vel)


class Percussion:
    """SSG noise -> GM drum kit.

    The AY noise channel is a hiss whose pitch is set by register 0x06 and
    whose loudness follows the volume register.  A MIDI drum note cannot
    change timbre mid-note, so a noise swell is written as a roll of hits
    whose velocity follows the volume -- which is how the same effect is
    played on a real kit.
    """

    def __init__(self, min_gap_ms=25.0, hit_ms=20.0):
        self.events = []
        self.min_gap = min_gap_ms / 1000.0 * SAMPLE_RATE
        self.hit_ms = hit_ms
        self._last = -1e9

    def hit(self, t, note, velocity):
        if velocity <= 0 or t - self._last < self.min_gap:
            return
        self._last = t
        self.events.append((t, note, velocity, self.hit_ms))


# ---------------------------------------------------------------------------
# MIDI serialisation
# ---------------------------------------------------------------------------

def vlq(value: int) -> bytes:
    value = max(0, value)
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def meta(kind: int, payload: bytes) -> bytes:
    return b"\xFF" + bytes([kind]) + vlq(len(payload)) + payload


def midi_track(name, channel, program, events):
    body = bytearray()
    body += vlq(0) + meta(0x03, name.encode("utf-8", "replace"))
    if program is not None:
        body += vlq(0) + bytes([0xC0 | channel, program])
        body += vlq(0) + bytes([0xB0 | channel, 11, 100])
        for cc, val in ((101, 0), (100, 0), (6, int(BEND_RANGE_SEMITONES)),
                        (101, 127), (100, 127)):
            body += vlq(0) + bytes([0xB0 | channel, cc, val])
    last = 0
    for tick, _order, data in sorted(events, key=lambda e: (e[0], e[1])):
        body += vlq(tick - last) + data
        last = tick
    body += vlq(0) + b"\xFF\x2F\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def smf(tracks):
    return (b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), PPQ)
            + b"".join(tracks))


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def expand_loop(stream, loops):
    """Split the write stream into intro + loop and repeat the loop."""
    intro, body = [], []
    loop_t = None
    end_t = 0.0
    for item in stream:
        t, cmd = item[0], item[1]
        if cmd == "loop":
            loop_t = t
        elif cmd == "end":
            end_t = t
            break
        elif loop_t is None:
            intro.append(item)
        else:
            body.append(item)
    if loop_t is None or loops <= 1 or not body:
        return intro + body, end_t, loop_t
    span = end_t - loop_t
    out = list(intro)
    for i in range(loops):
        shift = loop_t + i * span
        for (t, cmd, reg, val) in body:
            out.append((t - loop_t + shift, cmd, reg, val))
    return out, loop_t + loops * span, loop_t


def burst_flags(writes, window=12):
    """Mark the last write of each pitch/value burst as 'settled'.

    Drivers write 16-bit values one byte at a time, a couple of samples
    apart, so the intermediate state is meaningless (an SSG tone period of
    low=0x77, high=stale, then low=0x77, high=0x01).  Everything but the
    last write of such a burst is flagged so channels latch it without
    emitting a pitch or level change.
    """
    settled = [True] * len(writes)
    last = {}
    for i, (t, cmd, reg, val) in enumerate(writes):
        if cmd in (0x55, 0x56, 0x57, 0x58):
            if reg <= 0x05:
                key = (cmd, "ssgper", reg >> 1)
            elif reg <= 0x0F:
                key = (cmd, "ssgvol", reg)
            elif 0xA0 <= reg <= 0xAE:
                key = (cmd, "freq", reg & 0x03)
            elif 0x40 <= reg <= 0x9E:
                key = (cmd, "lvl", (reg - 0x40) & 0x03)
            else:
                key = None
        elif cmd in (0x52, 0x53):
            key = (cmd, "freq", reg & 0x03) if 0xA0 <= reg <= 0xAE else (
                (cmd, "lvl", (reg - 0x40) & 0x03) if 0x40 <= reg <= 0x9E else None)
        else:
            key = None
        if key is not None:
            prev = last.get(key)
            if prev is not None and t - writes[prev][0] <= window:
                settled[prev] = False
            last[key] = i
    return settled


def convert(path, out_path, loops=1, verbose=False, programs=None, release=True,
            use_drums=True, drum_override=None):
    vgm = Vgm(path)
    chip = vgm.chip()
    if chip is None:
        raise ValueError("%s: no YM2203/YM2608/YM2612 chip in header" % path)
    clock = vgm.clocks[chip]
    # the SSG half of a YM2203/YM2608 is clocked at half the FM clock
    ssg_clock = clock / 2.0 if chip in ("YM2203", "YM2608") else 0.0

    writes, total_t, loop_t = expand_loop(vgm.stream(), loops)
    settled = burst_flags(writes)

    fm = [FmChannel(i, "FM %d" % (i + 1), clock) for i in range(3)]
    drums = Percussion() if (ssg_clock and use_drums) else None
    ssg = ([SsgChannel(i, "SSG %s" % "ABC"[i], ssg_clock, drums, drum_override)
            for i in range(3)] if ssg_clock else [])
    channels = fm + ssg
    for c in channels:
        c.use_release = release

    for i, (t, cmd, reg, val) in enumerate(writes):
        ok = settled[i]
        if cmd in (0x55, 0x57):               # YM2203 / YM2608 port 0
            if reg <= 0x0F:
                for c in ssg:
                    c.write(t, reg, val, ok)
            else:
                for c in fm:
                    c.write(t, reg, val, ok)
        elif cmd in (0x56, 0x58):             # port 1: register bank 0x100+
            pass
        elif cmd in (0x52, 0x53):             # YM2612
            for c in fm:
                c.write(t, reg, val, ok)

    for c in channels:
        c.note_off(total_t)

    for c in fm:
        notes = [e[2] for e in c.events if e[1] == "on"]
        avg = sum(notes) / len(notes) if notes else 60.0
        c.program = PROG_BASS if avg < 50 else (PROG_EPIANO if avg < 72 else PROG_SAW)
    if programs:
        for i, c in enumerate(fm):
            if i < len(programs):
                c.program = programs[i]

    def to_ticks(events):
        out = []
        drop = set()
        last_on = {}
        last_on_time = {}
        on_times = [t for (t, action, a, b) in events if action == "on"]
        oi = 0
        for seq, (t, action, a, b) in enumerate(events):
            tick = int(round(t * TICKS_PER_SEC / SAMPLE_RATE))
            # `seq` breaks ties within a tick, so a note that is retriggered
            # at the same tick keeps its off-before-on order
            if action == "on":
                out.append((tick, seq, bytes([0x90, a, b])))
                last_on[a] = len(out) - 1
                last_on_time[a] = t
            elif action == "off":
                # hold on through the release tail, but never into the next
                # note on this channel (it would overlap itself)
                end = t + b * SAMPLE_RATE / 1000.0
                while oi < len(on_times) and on_times[oi] <= t:
                    oi += 1
                if oi < len(on_times):
                    end = min(end, on_times[oi])
                end = min(end, total_t)
                on_idx = last_on.get(a)
                if on_idx is not None and (t <= last_on_time.get(a, -1.0)
                                           or tick <= out[on_idx][0]):
                    # key-on and key-off in the same instant, or close enough
                    # to land on the same tick (a mixer switch, a burst write):
                    # the envelope never starts, so drop it instead of writing
                    # a click-length note
                    drop.add(last_on.pop(a))
                    last_on_time.pop(a, None)
                    continue
                end_tick = max(tick, ticks(end))
                out.append((end_tick, seq, bytes([0x80, a, 0])))
                last_on.pop(a, None)
                last_on_time.pop(a, None)
            elif action == "bend":
                out.append((tick, seq, bytes([0xE0, a & 0x7F, (a >> 7) & 0x7F])))
            elif action == "expr":
                out.append((tick, seq, bytes([0xB0, 11, a])))
        return [e for i, e in enumerate(out) if i not in drop]

    def ticks(t):
        return int(round(t * TICKS_PER_SEC / SAMPLE_RATE))

    head = bytearray()
    label = "vgm2mid_opn: %s" % os.path.basename(path)
    head += vlq(0) + meta(0x03, label.encode("utf-8", "replace"))
    head += vlq(0) + meta(0x51, struct.pack(">I", TEMPO_US)[1:])
    head += vlq(0) + meta(0x58, bytes([4, 2, 24, 8]))
    title = vgm.gd3.get("track_en") or vgm.gd3.get("track_jp")
    if title:
        head += vlq(0) + meta(0x01, title.encode("utf-8", "replace"))
    game = vgm.gd3.get("game_en") or vgm.gd3.get("game_jp")
    if game:
        head += vlq(0) + meta(0x02, game.encode("utf-8", "replace"))
    cursor = 0
    if loop_t:
        head += vlq(ticks(loop_t) - cursor) + meta(0x06, b"loopStart")
        cursor = ticks(loop_t)
    head += vlq(ticks(total_t) - cursor) + b"\xFF\x2F\x00"
    tracks = [b"MTrk" + struct.pack(">I", len(head)) + bytes(head)]

    midi_ch = 0
    for c in channels:
        if not c.events:
            continue
        if midi_ch == 9:                      # keep the drum channel free
            midi_ch = 10
        tracks.append(midi_track(c.name, midi_ch, c.program, to_ticks(c.events)))
        midi_ch += 1

    if drums and drums.events:
        dev = []
        for seq, (t, note, vel, hit_ms) in enumerate(drums.events):
            on = ticks(t)
            off = max(on + 1, ticks(t + hit_ms * SAMPLE_RATE / 1000.0))
            dev.append((on, seq * 2, bytes([0x99, note, vel])))
            dev.append((off, seq * 2 + 1, bytes([0x89, note, 0])))
        tracks.append(midi_track("Percussion (SSG noise)", 9, None, dev))

    with open(out_path, "wb") as fp:
        fp.write(smf(tracks))

    if verbose:
        print("  chip:     %s @ %d Hz (%d FM ch, SSG @ %.0f Hz)"
              % (chip, clock, 3, ssg_clock))
        print("  duration: %.2f s, %d loop(s)" % (total_t / SAMPLE_RATE, loops))
        for c in channels:
            ons = [e for e in c.events if e[1] == "on"]
            if ons:
                pitches = [e[2] for e in ons]
                print("  %-8s %4d notes  range %3d..%3d  program %2d"
                      % (c.name, len(ons), min(pitches), max(pitches), c.program))
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert VGM/VGZ (YM2203 / YM2608 / YM2612) to MIDI.")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", default=None,
                    help="output file (one input) or directory (several)")
    ap.add_argument("--loops", type=int, default=1,
                    help="how many times to play the looped section (default 1)")
    ap.add_argument("--stats", action="store_true", help="per-channel summary")
    ap.add_argument("--noise-drum", type=int, default=None, metavar="N",
                    help="force one GM drum note for SSG noise (default: pick by noise period)")
    ap.add_argument("--no-drums", action="store_true",
                    help="drop SSG noise instead of mapping it to the drum channel")
    ap.add_argument("--no-release", action="store_true",
                    help="end notes at key-off instead of holding through the carrier release")
    ap.add_argument("--programs", default=None,
                    help="comma-separated GM programs for FM 1..3 (overrides the heuristic)")
    args = ap.parse_args(argv)

    if args.loops < 1:
        ap.error("--loops must be >= 1")

    outdir = None
    if args.out and (len(args.inputs) > 1 or os.path.isdir(args.out)):
        outdir = args.out
        os.makedirs(outdir, exist_ok=True)

    progs = [int(x) for x in args.programs.split(",")] if args.programs else None
    failures = 0
    for path in args.inputs:
        base = os.path.splitext(os.path.basename(path))[0] + ".mid"
        target = (os.path.join(outdir, base) if outdir
                  else args.out if args.out
                  else os.path.join(os.path.dirname(path) or ".", base))
        try:
            convert(path, target, loops=args.loops, verbose=args.stats,
                    programs=progs, release=not args.no_release,
                    use_drums=not args.no_drums,
                    drum_override=args.noise_drum)
            print("%s -> %s (%d bytes)" % (path, target, os.path.getsize(target)))
        except Exception as exc:
            failures += 1
            print("%s: %s" % (path, exc), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
