"""Tests for vgm2mid_opn.

Everything here is synthetic: the VGM files are generated in-process from the
specification, so the suite needs no game rips and makes no copyright claims on
anything.  Run with either

    python3 -m unittest discover -s tests -v
    python3 -m pytest tests -q
"""

import contextlib
import gzip
import io
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vgm2mid_opn as v2m  # noqa: E402

HEADER_LEN = 0x100
CLOCK = 3993600
PPQ = 480
TICKS_PER_SEC = 960


# ---------------------------------------------------------------------------
# a tiny VGM writer, so tests can state exactly what the chip was told
# ---------------------------------------------------------------------------

class Builder:
    """Build a YM2203 VGM command stream."""

    def __init__(self, clock=CLOCK):
        self.body = bytearray()
        self.samples = 0
        self.clock = clock
        self.loop_off = None

    def write(self, reg, val):
        self.body += bytes((0x55, reg, val))
        return self

    def wait(self, samples):
        while samples > 0:
            chunk = min(samples, 0xFFFF)
            self.body += bytes((0x61, chunk & 0xFF, chunk >> 8))
            self.samples += chunk
            samples -= chunk
        return self

    def mark_loop(self):
        self.loop_off = len(self.body)
        return self

    def fm_patch(self, ch=0, rr=15, sl=0, tl=0):
        """Minimal 4-operator patch: algorithm 7 (all operators are carriers)."""
        for op in range(4):
            self.write(0x30 + op * 4 + ch, 0x01)        # MUL=1
            self.write(0x40 + op * 4 + ch, tl)          # TL
            self.write(0x50 + op * 4 + ch, 0x1F)        # AR=31
            self.write(0x60 + op * 4 + ch, 0x00)        # DR=0
            self.write(0x70 + op * 4 + ch, 0x00)        # SR=0
            self.write(0x80 + op * 4 + ch, (sl << 4) | rr)
        self.write(0xB0 + ch, 0x38)                     # algorithm 7
        return self

    def fm_note(self, ch, fnum, block, hold):
        self.write(0xA4 + ch, ((block & 7) << 3) | ((fnum >> 8) & 7))
        self.write(0xA0 + ch, fnum & 0xFF)
        self.write(0x28, 0xF0 | ch)
        self.wait(hold)
        self.write(0x28, 0x00 | ch)
        return self

    def ssg_mixer(self, val):
        return self.write(0x07, val)

    def ssg_tone(self, ch, period):
        self.write(0x00 + ch * 2, period & 0xFF)
        self.write(0x01 + ch * 2, (period >> 8) & 0x0F)
        return self

    def ssg_volume(self, ch, level):
        return self.write(0x08 + ch, level & 0x1F)

    def build(self, gd3=None, version=0x151):
        data = bytes(self.body) + b"\x66"
        hdr = bytearray(HEADER_LEN)
        hdr[0:4] = b"Vgm "
        struct.pack_into("<I", hdr, 0x08, version)
        struct.pack_into("<I", hdr, 0x18, self.samples)
        struct.pack_into("<I", hdr, 0x34, HEADER_LEN - 0x34)
        struct.pack_into("<I", hdr, 0x44, self.clock)
        if self.loop_off is not None:
            struct.pack_into("<I", hdr, 0x1C, HEADER_LEN + self.loop_off - 0x1C)
        out = bytearray(hdr) + data
        if gd3 is not None:
            tag = b"Gd3 " + struct.pack("<II", 0x100, len(gd3)) + gd3
            struct.pack_into("<I", out, 0x14, len(out) - 0x14)
            out += tag
        struct.pack_into("<I", out, 0x04, HEADER_LEN + len(data))
        return bytes(out)


# ---------------------------------------------------------------------------
# a small, independent Standard MIDI File reader
# ---------------------------------------------------------------------------

def _vlq(b, i):
    v = 0
    while True:
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i


def _track_events(body):
    i = 0
    tick = 0
    ev = []
    while i < len(body):
        delta, i = _vlq(body, i)
        tick += delta
        status = body[i]
        if status == 0xFF:
            kind = body[i + 1]
            ln, j = _vlq(body, i + 2)
            ev.append((tick, "meta", kind, body[j:j + ln]))
            i = j + ln
            if kind == 0x2F:
                break
        elif status & 0xF0 == 0x90:
            ev.append((tick, "on", body[i + 1], body[i + 2]))
            i += 3
        elif status & 0xF0 == 0x80:
            ev.append((tick, "off", body[i + 1], 0))
            i += 3
        elif status & 0xF0 == 0xC0:
            ev.append((tick, "program", body[i + 1], 0))
            i += 2
        elif status & 0xF0 == 0xE0:
            ev.append((tick, "bend", body[i + 1] | (body[i + 2] << 7), 0))
            i += 3
        elif status & 0xF0 == 0xB0:
            ev.append((tick, "cc", body[i + 1], body[i + 2]))
            i += 3
        else:
            raise AssertionError("unexpected status 0x%02x" % status)
    return ev


def parse_smf(path):
    with open(path, "rb") as fp:
        data = fp.read()
    assert data[:4] == b"MThd"
    ln, fmt, ntr, div = struct.unpack(">IHHH", data[4:14])
    assert ln == 6
    pos = 14
    tracks = []
    for _ in range(ntr):
        assert data[pos:pos + 4] == b"MTrk"
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        tracks.append(_track_events(data[pos + 8:pos + 8 + size]))
        pos += 8 + size
    assert pos == len(data), "trailing bytes after last track"
    return fmt, div, tracks


def track_name(events):
    for _tick, kind, a, data in events:
        if kind == "meta" and a == 0x03:
            return data.decode("utf-8", "replace")
    return ""


def named_tracks(tracks):
    return {track_name(t): t for t in tracks if track_name(t)}


def notes_of(events):
    """[(on_tick, off_tick, note, velocity)] for one track."""
    open_notes = {}
    out = []
    for tick, kind, a, b in events:
        if kind == "on" and b > 0:
            open_notes.setdefault(a, []).append((tick, b))
        elif kind == "off":
            if open_notes.get(a):
                on_tick, vel = open_notes[a].pop(0)
                out.append((on_tick, tick, a, vel))
    return out


def last_tick(events):
    return max(t for t, kind, a, b in events if kind == "meta" and a == 0x2F)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def vgm(self, builder, gd3=None):
        p = self.path("in.vgm")
        with open(p, "wb") as fp:
            fp.write(builder.build(gd3))
        return p

    def convert(self, vgm_path, **kw):
        out = self.path("out.mid")
        v2m.convert(vgm_path, out, **kw)
        return parse_smf(out)


# ---------------------------------------------------------------------------
# container / header handling
# ---------------------------------------------------------------------------

class TestContainer(Base):
    def test_detects_chip_and_clock(self):
        p = self.vgm(Builder().wait(100))
        v = v2m.Vgm(p)
        self.assertEqual(v.chip(), "YM2203")
        self.assertEqual(v.clocks["YM2203"], CLOCK)

    def test_gzip_is_decompressed(self):
        raw = Builder().wait(100).build()
        p = self.path("in.vgz")
        with gzip.open(p, "wb") as fp:
            fp.write(raw)
        self.assertEqual(v2m.Vgm(p).chip(), "YM2203")

    def test_header_pointers_are_relative_to_their_field(self):
        # data offset is stored relative to 0x34, GD3 relative to 0x14,
        # loop offset relative to 0x1C.  Reading them as absolute offsets
        # silently works on some files and breaks on others.
        b = Builder()
        b.wait(1000).mark_loop()
        b.wait(1000)
        gd3 = "\x00".join(["T", "テ", "G", "ゲ", "", "", "", "", "", "", ""])
        p = self.vgm(b, gd3=gd3.encode("utf-16-le"))
        v = v2m.Vgm(p)
        self.assertEqual(v.total_samples, 2000)
        self.assertGreater(v.loop_offset, HEADER_LEN)
        self.assertEqual(v.gd3["track_en"], "T")

    def test_gd3_is_english_first(self):
        b = Builder().wait(100)
        gd3 = "\x00".join(["English title", "日本語タイトル",
                           "English game", "日本語ゲーム",
                           "", "", "", "", "", "", ""])
        v = v2m.Vgm(self.vgm(b, gd3=gd3.encode("utf-16-le")))
        self.assertEqual(v.gd3["track_en"], "English title")
        self.assertEqual(v.gd3["track_jp"], "日本語タイトル")

    def test_no_supported_chip_raises(self):
        b = Builder(clock=0).wait(100)
        p = self.vgm(b)
        self.assertIsNone(v2m.Vgm(p).chip())
        with self.assertRaises(ValueError):
            v2m.convert(p, self.path("x.mid"))


# ---------------------------------------------------------------------------
# pitch decoding
# ---------------------------------------------------------------------------

class TestPitch(Base):
    def test_fm_fnum_block_to_note(self):
        # The manual's fn = FN * fM / (144 * 2^(20-B)).  fnum 1040 at block 4
        # is 440 Hz.  This is the test that catches an octave error, which the
        # "does it land on a semitone" check cannot see.
        for fnum, block, expected in ((1040, 4, 69), (1040, 3, 57),
                                      (1040, 5, 81), (500, 4, 56)):
            b = Builder().fm_patch().fm_note(0, fnum, block, 4000).wait(100)
            _fmt, _div, tracks = self.convert(self.vgm(b))
            notes = notes_of(named_tracks(tracks)["FM 1"])
            self.assertEqual(len(notes), 1, (fnum, block))
            self.assertEqual(notes[0][2], expected, (fnum, block))

    def test_ssg_period_to_note(self):
        # SSG runs at half the FM clock: f = (fM/2) / (16 * TP)
        period = 284                       # 1996800/(16*284) = 439.6 Hz
        b = (Builder().ssg_mixer(0x38).ssg_tone(0, period).wait(4)
             .ssg_volume(0, 15).wait(4000))
        _fmt, _div, tracks = self.convert(self.vgm(b))
        notes = notes_of(named_tracks(tracks)["SSG A"])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][2], 69)

    def test_pitch_bend_tracks_vibrato(self):
        b = Builder().fm_patch()
        b.write(0xA4, (4 << 3) | ((1040 >> 8) & 7)).write(0xA0, 1040 & 0xFF)
        b.write(0x28, 0xF0).wait(2000)
        b.write(0xA0, 1044 & 0xFF)          # nudge fnum mid-note (~7 cents)
        b.write(0xA4, (4 << 3) | ((1044 >> 8) & 7))
        b.wait(2000)
        b.write(0x28, 0x00)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        bends = [a for t, kind, a, b2 in named_tracks(tracks)["FM 1"] if kind == "bend"]
        self.assertEqual(len(bends), 2)
        self.assertTrue(any(b != 8192 for b in bends))

    def test_unsettled_write_is_latched_but_not_applied(self):
        # convert() flags the first byte of a two-byte write as unsettled.
        # It must still be latched, or the second byte completes a stale value.
        ch = v2m.SsgChannel(0, "SSG A", 1996800)
        ch.write(0, 0x00, 0x77)
        ch.write(2, 0x01, 0x01)             # period 375
        ch.write(1000, 0x08, 15)            # note on, ~23 ms in
        before = len(ch.events)
        ch.write(5000, 0x00, 0xEC, settled=False)
        self.assertEqual(len(ch.events), before,
                         "half-written period must not be applied")
        self.assertEqual(ch.period, (0x01 << 8) | 0xEC,
                         "but the byte must still be latched")
        ch.write(5002, 0x01, 0x00)
        self.assertGreater(len(ch.events), before,
                           "completed period should update the pitch")
        self.assertEqual(ch.period, 0xEC)

    def test_burst_flags_only_settle_the_last_write(self):
        # (time, command, register, value) with two bytes of one value
        writes = [(0, 0x55, 0x04, 0x77), (2, 0x55, 0x05, 0x01),
                  (100, 0x40, 0x00, 0x10), (400, 0x55, 0x28, 0xF0)]
        settled = v2m.burst_flags(writes)
        self.assertEqual(settled, [False, True, True, True])


# ---------------------------------------------------------------------------
# note segmentation
# ---------------------------------------------------------------------------

class TestNotes(Base):
    def test_key_on_off_become_note(self):
        b = Builder().fm_patch().fm_note(0, 1040, 4, 4000).wait(100)
        _fmt, _div, tracks = self.convert(self.vgm(b), release=False)
        notes = notes_of(named_tracks(tracks)["FM 1"])
        self.assertEqual(len(notes), 1)
        on, off, note, vel = notes[0]
        self.assertEqual(off - on, round(4000 * TICKS_PER_SEC / 44100.0), 1)
        self.assertGreater(vel, 100)        # TL=0 is the loudest possible

    def test_zero_length_note_is_dropped(self):
        b = Builder().fm_patch().fm_note(0, 1040, 4, 0).wait(4000)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        self.assertEqual(notes_of(named_tracks(tracks)["FM 1"]), [])

    def test_release_tail_does_not_overlap_next_note(self):
        b = (Builder().fm_patch(rr=4, sl=8)
             .fm_note(0, 1040, 4, 1000)     # long release
             .wait(500))
        b.write(0x28, 0xF0).wait(2000)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        notes = notes_of(named_tracks(tracks)["FM 1"])
        self.assertEqual(len(notes), 2)
        first_off, second_on = notes[0][1], notes[1][0]
        self.assertLessEqual(first_off, second_on, "notes must not overlap")
        self.assertGreater(first_off, notes[0][0] + 20,
                           "release should extend the first note")

    def test_no_release_flag_cuts_at_key_off(self):
        b = (Builder().fm_patch(rr=4, sl=8)
             .fm_note(0, 1040, 4, 1000).wait(5000))
        _fmt, _div, tracks = self.convert(self.vgm(b), release=False)
        notes = notes_of(named_tracks(tracks)["FM 1"])
        self.assertEqual(notes[0][1] - notes[0][0],
                         round(1000 * TICKS_PER_SEC / 44100.0), 1)


# ---------------------------------------------------------------------------
# SSG noise -> drums
# ---------------------------------------------------------------------------

class TestNoise(Base):
    def _noise_swell(self, levels=(5, 8, 11)):
        b = Builder().ssg_mixer(0x38).ssg_tone(2, 300).wait(4).ssg_volume(2, 10).wait(500)
        b.ssg_mixer(0x1C)                   # switch C to noise
        b.write(0x06, 6)                    # noise period
        for level in levels:
            b.ssg_volume(2, 0).wait(2).ssg_volume(2, level).wait(1500)
        return b

    def test_noise_goes_to_the_drum_channel(self):
        _fmt, _div, tracks = self.convert(self.vgm(self._noise_swell()))
        named = named_tracks(tracks)
        self.assertIn("Percussion (SSG noise)", named)
        hits = notes_of(named["Percussion (SSG noise)"])
        self.assertEqual(len(hits), 3)
        self.assertTrue(all(n == 57 for _o, _f, n, _v in hits),
                        "bright noise should pick a cymbal")
        self.assertEqual([v for _o, _f, _n, v in hits],
                         sorted(v for _o, _f, _n, v in hits),
                         "velocity should follow the swell")

    def test_noise_silences_the_tone_channel(self):
        _fmt, _div, tracks = self.convert(self.vgm(self._noise_swell()))
        # one tone note before the switch, none during the noise
        during = [n for on, off, n, v in notes_of(named_tracks(tracks)["SSG C"])
                  if on > (500 * TICKS_PER_SEC / 44100.0) + 2]
        self.assertEqual(during, [])

    def test_drum_override(self):
        _fmt, _div, tracks = self.convert(self.vgm(self._noise_swell()),
                                          drum_override=42)
        hits = notes_of(named_tracks(tracks)["Percussion (SSG noise)"])
        self.assertTrue(hits)
        self.assertTrue(all(n == 42 for _o, _f, n, _v in hits))

    def test_tone_period_is_latched_during_noise(self):
        # The driver keeps writing the tone period while the mixer is on noise
        # and the chip sounds it the instant the mixer returns.  Dropping those
        # writes leaves a stale period and the next note squeaks.
        b = Builder().ssg_mixer(0x38).ssg_tone(2, 60).wait(4).ssg_volume(2, 10).wait(500)
        b.ssg_mixer(0x1C).write(0x06, 6)
        b.ssg_volume(2, 0).wait(2).ssg_volume(2, 9).wait(300)
        b.ssg_tone(2, 284)                  # set up the note to come back to
        b.wait(300)
        b.ssg_mixer(0x38)                   # back to tone
        b.write(0x0A, 10).wait(2000)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        notes = notes_of(named_tracks(tracks)["SSG C"])
        self.assertEqual(notes[-1][2], 69, "should use the period set during noise")

    def test_no_drums_flag_drops_noise(self):
        _fmt, _div, tracks = self.convert(self.vgm(self._noise_swell()),
                                          use_drums=False)
        self.assertNotIn("Percussion (SSG noise)", named_tracks(tracks))


# ---------------------------------------------------------------------------
# file structure
# ---------------------------------------------------------------------------

class TestOutput(Base):
    def test_smf_structure(self):
        b = Builder().fm_patch().fm_note(0, 1040, 4, 2000).wait(88200 - 2000)
        fmt, div, tracks = self.convert(self.vgm(b))
        self.assertEqual(fmt, 1)
        self.assertEqual(div, PPQ)
        self.assertGreaterEqual(len(tracks), 2)          # conductor + channels
        for t in tracks:
            self.assertEqual(t[-1][1:3], ("meta", 0x2F))  # ends with EOT

    def test_length_matches_the_log(self):
        b = Builder().fm_patch().fm_note(0, 1040, 4, 1000).wait(88200 - 1000)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        self.assertEqual(last_tick(tracks[0]), 2 * TICKS_PER_SEC)

    def test_loop_is_repeated(self):
        b = Builder().fm_patch().fm_note(0, 1040, 4, 1000).wait(21050)
        b.mark_loop()
        b.fm_note(0, 1040, 4, 1000).wait(43050)          # loop is 1.0 s
        one = self.convert(self.vgm(b))
        two = self.convert(self.vgm(b), loops=2)
        self.assertAlmostEqual(last_tick(one[2][0]) / TICKS_PER_SEC, 1.5, places=1)
        self.assertAlmostEqual(last_tick(two[2][0]) / TICKS_PER_SEC, 2.5, places=1)

    def test_tempo_is_120bpm(self):
        b = Builder().wait(1000)
        _fmt, _div, tracks = self.convert(self.vgm(b))
        tempo = [data for _t, kind, a, data in tracks[0]
                 if kind == "meta" and a == 0x51]
        self.assertEqual(tempo[0], b"\x07\xa1\x20")       # 500000 us/quarter

    def test_cli_writes_a_file(self):
        p = self.vgm(Builder().fm_patch().fm_note(0, 1040, 4, 500).wait(500))
        out = self.path("cli.mid")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(v2m.main([p, "-o", out]), 0)
        self.assertTrue(os.path.getsize(out) > 0)

    def test_cli_reports_failures(self):
        p = self.path("bad.vgm")
        with open(p, "wb") as fp:
            fp.write(b"not a vgm at all")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(v2m.main([p, "-o", self.path("x.mid")]), 1)


if __name__ == "__main__":
    unittest.main()
