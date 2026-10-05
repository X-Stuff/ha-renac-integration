"""The framer on streams of the protocol's frames with made-up values, the mock cloud's requests and the fake module's
replies to them: whole and split, padded and corrupted."""

from __future__ import annotations

import random

from renac_protocol.framer import Framer
from renac_protocol.frames import AA55Frame, ModbusFrame, ModbusKind, build_aa55_frame, build_read_reply
from tests.mocks.mock_cloud import BAD_CRC_READ, cloud_frames
from tests.mocks.mock_hf_lpt270 import FakeModule, SimulatedInverter, initial_words, random_identity

# A one-word reply with its first three bytes mangled and one byte lost: garbage.
GOOD = build_read_reply((0x1234,))
MANGLED = b"\xa0\x20\x20" + build_read_reply((1,))[4:]


def cloud_requests() -> list[bytes]:
    """The mock cloud's first poll, the bad-CRC read and the write outside the clock included."""
    return [request for _, request in cloud_frames()]


def module_replies() -> list[bytes]:
    """A fake module's replies to the cloud's requests, in order; the requests it leaves unanswered have none."""
    identity = random_identity()
    inverter = SimulatedInverter(initial_words(identity))
    inverter.advance(0)
    module = FakeModule(inverter, identity=identity, answers_handshake=True)
    return [reply for request in cloud_requests() for _, reply in module.handle(request, 0.0) if reply is not None]


def feed_whole(framer: Framer, chunks: list[bytes], start: float = 0.0, step: float = 0.01) -> list:
    frames = []
    now = start
    for chunk in chunks:
        frames += framer.feed(chunk, now)
        now += step
    return frames


def test_the_cloud_stream_has_one_bad_frame() -> None:
    requests = cloud_requests()
    framer = Framer(from_master=True)
    frames = feed_whole(framer, requests)

    assert [frame.raw for frame in frames] == [request for request in requests if request != BAD_CRC_READ]
    assert framer.counters.crc_errors == 1
    assert framer.counters.garbage_bytes + framer.counters.padding_bytes == len(BAD_CRC_READ)
    assert framer.pending_bytes == 0


def test_the_cloud_stream_with_pass_bad_crc_hands_the_bad_frame_on_whole() -> None:
    requests = cloud_requests()
    framer = Framer(from_master=True, pass_bad_crc=True)
    frames = feed_whole(framer, requests)

    assert len(frames) == len(requests)
    bad = [f for f in frames if isinstance(f, ModbusFrame) and not f.crc_ok]
    assert len(bad) == 1 and bad[0].raw == BAD_CRC_READ
    assert (bad[0].kind, bad[0].address, bad[0].count) == (ModbusKind.READ_REQUEST, 11121, 3)
    assert framer.counters.crc_errors == 1 and framer.counters.garbage_bytes == 0
    assert framer.counters.frames == len(requests) - 1


def test_pass_bad_crc_still_checks_the_aa55_checksum() -> None:
    broken = bytearray(bytes.fromhex("aa55010000fe1500000213"))
    broken[-1] ^= 0x01
    good = bytes.fromhex("01 03 27 10 00 20 4f 63")
    framer = Framer(from_master=True, pass_bad_crc=True)
    frames = framer.feed(bytes(broken) + good, 0.0)
    assert [f.raw for f in frames] == [good]
    assert framer.counters.crc_errors == 1


def test_the_device_stream_joined_with_splits_and_padding() -> None:
    replies = module_replies()
    rng = random.Random()
    stream = bytearray()
    padding = 0
    for reply in replies:
        pad = bytes(rng.choice([0x00, 0xFF]) for _ in range(rng.randint(0, 4)))
        padding += len(pad)
        stream += pad + reply
    chunks = []
    at = 0
    while at < len(stream):
        size = rng.randint(1, 60)
        chunks.append(bytes(stream[at : at + size]))
        at += size

    framer = Framer(from_master=False)
    frames = feed_whole(framer, chunks, step=0.001)
    assert [f.raw for f in frames] == replies
    assert framer.counters.padding_bytes == padding
    assert framer.counters.crc_errors == 0
    assert framer.counters.garbage_bytes == 0


def test_one_reply_byte_by_byte() -> None:
    reply = max(module_replies(), key=len)
    framer = Framer(from_master=False)
    frames = []
    for i, byte in enumerate(reply):
        frames += framer.feed(bytes([byte]), i * 0.001)
    assert len(frames) == 1 and frames[0].raw == reply


def test_a_corrupted_frame_resyncs_to_the_next_one() -> None:
    good = build_read_reply((26, 1, 2, 3, 4, 5))
    broken = bytearray(good)
    broken[5] ^= 0x40
    framer = Framer(from_master=False)
    frames = framer.feed(bytes(broken) + good, 0.0)
    assert len(frames) == 1 and frames[0].raw == good
    assert framer.counters.crc_errors == 1
    assert framer.counters.garbage_bytes + framer.counters.padding_bytes == len(good)


def test_garbage_between_frames_is_counted_once_per_byte() -> None:
    framer = Framer(from_master=False)
    frames = framer.feed(MANGLED + GOOD, 0.0)
    assert len(frames) == 1 and frames[0].raw == GOOD
    assert framer.counters.garbage_bytes == len(MANGLED)


def test_a_partial_frame_is_dropped_after_the_idle_timeout() -> None:
    framer = Framer(from_master=False)
    assert framer.feed(GOOD[:3], 0.0) == []
    assert framer.pending_bytes == 3
    frames = framer.feed(GOOD, 1.0)
    assert len(frames) == 1 and frames[0].raw == GOOD
    assert framer.counters.partial_discards == 1
    assert framer.counters.garbage_bytes == 3


def test_flush_idle_without_new_bytes() -> None:
    framer = Framer(from_master=False)
    framer.feed(b"\x01\x03", 0.0)
    framer.flush_idle(0.2)
    assert framer.pending_bytes == 2
    framer.flush_idle(0.4)
    assert framer.pending_bytes == 0
    assert framer.counters.partial_discards == 1


def test_only_padding_leaves_nothing_pending() -> None:
    framer = Framer(from_master=False)
    assert framer.feed(b"\xff\xff\x00", 0.0) == []
    assert framer.pending_bytes == 0
    assert framer.counters.padding_bytes == 3


def test_aa55_split_across_the_length_byte() -> None:
    payload = b"\x01ABCDEFG"
    raw = build_aa55_frame(0x15, 0x0B, payload, reply=True)
    framer = Framer(from_master=False)
    assert framer.feed(raw[:8], 0.0) == []
    assert framer.feed(raw[8:10], 0.0) == []
    frames = framer.feed(raw[10:], 0.0)
    assert len(frames) == 1
    assert isinstance(frames[0], AA55Frame) and frames[0].payload == payload


def test_aa_not_followed_by_55_is_garbage() -> None:
    framer = Framer(from_master=False)
    frames = framer.feed(b"\xaa\x01" + GOOD, 0.0)
    assert len(frames) == 1
    assert framer.counters.garbage_bytes == 2
