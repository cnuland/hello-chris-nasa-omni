"""AC-PER-2 Window builder: last N keyframes (default 6) + the overlapping audio chunk, every stride seconds
(default 4); frames downscaled to the configured long side (default 448 px)."""
from __future__ import annotations

import itertools
from datetime import timedelta

import numpy as np
from support import T0, ts

from perception.media import downscale
from perception.types import AudioChunk, Keyframe
from perception.window import WindowBuilder


def kf(sec: float, h: int = 720, w: int = 1280) -> Keyframe:
    img = np.full((h, w, 3), int(sec * 10) % 255, dtype=np.uint8)
    return Keyframe(timestamp=ts(sec), image=img, pts=sec, index=int(sec))


def chunk(start: float, seconds: float = 5.0) -> AudioChunk:
    return AudioChunk(timestamp=ts(start), samples=np.zeros(int(seconds * 16000), dtype=np.int16))


def feed(builder, items):
    out = []
    for it in items:
        out.extend(builder.push(it))
    return out


def test_ac_per_02_defaults():
    b = WindowBuilder()
    assert (b.n_frames, b.stride_s, b.long_side) == (6, 4.0, 448)


def test_ac_per_02_emits_every_stride_seconds():
    windows = feed(WindowBuilder(), [kf(s) for s in range(20)])
    ends = [w.t_end for w in windows]
    assert ends == [ts(0), ts(4), ts(8), ts(12), ts(16)]
    assert all((b - a) == timedelta(seconds=4) for a, b in itertools.pairwise(ends))


def test_ac_per_02_window_holds_last_n_frames_in_order():
    windows = feed(WindowBuilder(), [kf(s) for s in range(20)])
    w = windows[-1]  # emitted at t=16
    assert [f.timestamp for f in w.frames] == [ts(s) for s in range(11, 17)]
    assert w.t_start == ts(11) and w.t_end == ts(16)


def test_ac_per_02_early_windows_have_fewer_frames():
    windows = feed(WindowBuilder(), [kf(s) for s in range(6)])
    assert len(windows[0].frames) == 1
    assert len(windows[1].frames) == 5  # t=0..4


def test_ac_per_02_configured_n_and_stride():
    windows = feed(WindowBuilder(n_frames=3, stride_s=2.0), [kf(s) for s in range(10)])
    assert [w.t_end for w in windows] == [ts(0), ts(2), ts(4), ts(6), ts(8)]
    assert all(len(w.frames) <= 3 for w in windows)
    assert [f.timestamp for f in windows[-1].frames] == [ts(6), ts(7), ts(8)]


def test_ac_per_02_irregular_frame_times_still_respect_stride():
    windows = feed(WindowBuilder(stride_s=4.0), [kf(s) for s in (0, 1.5, 3.9, 4.2, 7.0, 8.3, 9.0)])
    assert [w.t_end for w in windows] == [ts(0), ts(4.2), ts(8.3)]


def test_ac_per_02_flush_emits_pending_tail():
    b = WindowBuilder()
    windows = feed(b, [kf(s) for s in range(20)])
    tail = b.flush()
    assert len(windows) == 5 and len(tail) == 1
    assert tail[0].t_end == ts(19)
    assert [f.timestamp for f in tail[0].frames] == [ts(s) for s in range(14, 20)]
    assert b.flush() == []


def test_ac_per_02_flush_without_new_frames_is_empty():
    b = WindowBuilder()
    feed(b, [kf(0)])
    assert b.flush() == []


def test_ac_per_02_frames_downscaled_to_long_side():
    windows = feed(WindowBuilder(), [kf(0)])
    img = windows[0].frames[0].image
    assert img.shape == (252, 448, 3)
    assert img.dtype == np.uint8


def test_ac_per_02_portrait_frames_downscaled_on_height():
    windows = feed(WindowBuilder(long_side=448), [kf(0, h=1280, w=720)])
    assert windows[0].frames[0].image.shape == (448, 252, 3)


def test_ac_per_02_small_frames_not_upscaled():
    windows = feed(WindowBuilder(), [kf(0, h=240, w=320)])
    assert windows[0].frames[0].image.shape == (240, 320, 3)


def test_ac_per_02_window_frames_keep_timestamps_after_resize():
    w = feed(WindowBuilder(), [kf(s) for s in range(5)])[-1]
    assert [f.timestamp for f in w.frames] == [ts(s) for s in range(5)]


def test_ac_per_02_audio_chunk_with_largest_overlap_is_attached():
    items = [chunk(0), *[kf(s) for s in range(5)], chunk(5), *[kf(s) for s in range(5, 10)]]
    windows = feed(WindowBuilder(), items)
    # emitted at t=0, 4, 8 ; window @8 covers frames 3..8 -> chunk [5,10) overlaps 3 s vs [0,5) 2 s
    w8 = next(w for w in windows if w.t_end == ts(8))
    assert w8.audio is not None and w8.audio.timestamp == ts(5)
    w4 = next(w for w in windows if w.t_end == ts(4))
    assert w4.audio is not None and w4.audio.timestamp == ts(0)


def test_ac_per_02_window_without_overlapping_audio_has_none():
    windows = feed(WindowBuilder(), [chunk(-30), *[kf(s) for s in range(5)]])
    assert all(w.audio is None for w in windows)


def test_ac_per_02_no_audio_at_all_gives_none():
    windows = feed(WindowBuilder(), [kf(s) for s in range(5)])
    assert windows and all(w.audio is None for w in windows)


def test_ac_per_02_downscale_helper_keeps_aspect_and_never_upscales():
    big = np.zeros((1080, 1920, 3), dtype=np.uint8)
    assert downscale(big, 448).shape == (252, 448, 3)
    small = np.zeros((100, 200, 3), dtype=np.uint8)
    assert downscale(small, 448).shape == (100, 200, 3)


def test_ac_per_02_windows_are_indexed_sequentially():
    b = WindowBuilder(stride_s=1.0)
    windows = feed(b, [kf(s) for s in range(4)]) + b.flush()
    assert [w.index for w in windows] == list(range(len(windows)))
    assert windows[0].t_start == T0
