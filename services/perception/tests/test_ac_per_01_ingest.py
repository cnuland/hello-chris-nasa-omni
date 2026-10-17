"""AC-PER-1 Ingest: ffmpeg reads a file (looping allowed) or URL; keyframes at fps (default 1) and 16 kHz mono PCM
audio chunks (default 5 s); timestamps are wall-clock start_time + media pts, monotonic."""
from __future__ import annotations

import itertools
from datetime import timedelta

import numpy as np
import pytest
from support import T0

from perception.ingest import IngestError, ingest, iter_audio_chunks, iter_keyframes
from perception.types import AudioChunk, Keyframe


def _secs(dt):
    return (dt - T0).total_seconds()


def test_ac_per_01_keyframes_default_fps_is_one_per_second(sample_video):
    frames = list(iter_keyframes(str(sample_video), start_time=T0))
    assert 5 <= len(frames) <= 7, len(frames)
    assert all(isinstance(f, Keyframe) for f in frames)
    gaps = [_secs(b.timestamp) - _secs(a.timestamp) for a, b in itertools.pairwise(frames)]
    assert all(0.9 <= g <= 1.1 for g in gaps), gaps


def test_ac_per_01_keyframes_are_rgb_uint8_full_resolution(sample_video):
    frame = next(iter(iter_keyframes(str(sample_video), start_time=T0)))
    assert frame.image.dtype == np.uint8
    assert frame.image.shape == (240, 320, 3)
    assert frame.image.std() > 1.0, "decoded frame should not be blank (testsrc has a colour pattern)"


def test_ac_per_01_timestamps_are_start_time_plus_pts(sample_video):
    frames = list(iter_keyframes(str(sample_video), start_time=T0))
    for f in frames:
        assert f.timestamp.tzinfo is not None
        assert abs(_secs(f.timestamp) - f.pts) < 1e-3, (f.timestamp, f.pts)
    assert 0.0 <= frames[0].pts < 1.0


def test_ac_per_01_timestamps_strictly_monotonic(sample_video):
    frames = list(iter_keyframes(str(sample_video), start_time=T0))
    stamps = [f.timestamp for f in frames]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)


def test_ac_per_01_configured_fps_two(sample_video):
    frames = list(iter_keyframes(str(sample_video), start_time=T0, fps=2.0))
    assert 11 <= len(frames) <= 13, len(frames)
    gaps = [_secs(b.timestamp) - _secs(a.timestamp) for a, b in itertools.pairwise(frames)]
    assert all(0.4 <= g <= 0.6 for g in gaps), gaps


def test_ac_per_01_audio_chunks_16k_mono_pcm16_default_5s(sample_video):
    chunks = list(iter_audio_chunks(str(sample_video), start_time=T0))
    assert len(chunks) == 2, [c.duration_s for c in chunks]  # 5 s + ~1 s remainder
    first = chunks[0]
    assert isinstance(first, AudioChunk)
    assert first.sample_rate == 16000
    assert first.samples.dtype == np.int16 and first.samples.ndim == 1
    assert abs(len(first.samples) - 80000) <= 160
    assert 0 < chunks[1].duration_s <= 1.2


def test_ac_per_01_audio_chunk_timestamps_aligned_to_media_time(sample_video):
    chunks = list(iter_audio_chunks(str(sample_video), start_time=T0))
    assert abs(_secs(chunks[0].timestamp)) < 0.1
    assert abs(_secs(chunks[1].timestamp) - 5.0) < 0.1


def test_ac_per_01_audio_content_is_decoded_not_silence(sample_video):
    chunk = next(iter(iter_audio_chunks(str(sample_video), start_time=T0)))
    rms = float(np.sqrt(np.mean(chunk.samples.astype(np.float64) ** 2)))
    assert rms > 1000, rms


def test_ac_per_01_configured_chunk_length(sample_video):
    chunks = list(iter_audio_chunks(str(sample_video), start_time=T0, chunk_s=2.0))
    assert len(chunks) == 3
    assert all(abs(c.duration_s - 2.0) < 0.05 for c in chunks)
    assert [round(_secs(c.timestamp)) for c in chunks] == [0, 2, 4]


def test_ac_per_01_loop_keeps_wall_clock_monotonic(sample_video):
    frames = list(itertools.islice(iter_keyframes(str(sample_video), start_time=T0, loop=True), 15))
    assert len(frames) == 15
    stamps = [f.timestamp for f in frames]
    assert all(b > a for a, b in itertools.pairwise(stamps)), stamps
    assert _secs(stamps[-1]) > 10.0, "looping must keep advancing time past the media duration"


def test_ac_per_01_loop_with_max_loops_terminates(sample_video):
    frames = list(iter_keyframes(str(sample_video), start_time=T0, loop=True, max_loops=2))
    assert 10 <= len(frames) <= 14, len(frames)


def test_ac_per_01_reads_url_source(http_media_url):
    frames = list(iter_keyframes(f"{http_media_url}/sample.mp4", start_time=T0))
    assert 5 <= len(frames) <= 7
    chunks = list(iter_audio_chunks(f"{http_media_url}/sample.mp4", start_time=T0))
    assert len(chunks) == 2


def test_ac_per_01_video_without_audio_yields_no_chunks(video_only):
    assert list(iter_audio_chunks(str(video_only), start_time=T0)) == []
    assert 3 <= len(list(iter_keyframes(str(video_only), start_time=T0))) <= 5


def test_ac_per_01_missing_file_raises_ingest_error(tmp_path):
    with pytest.raises(IngestError):
        list(iter_keyframes(str(tmp_path / "nope.mp4"), start_time=T0))


def test_ac_per_01_not_a_video_raises_ingest_error(tmp_path):
    bogus = tmp_path / "bogus.mp4"
    bogus.write_bytes(b"this is not a video")
    with pytest.raises(IngestError):
        list(iter_keyframes(str(bogus), start_time=T0))


def test_ac_per_01_ingest_merges_frames_and_audio_in_time_order(sample_video):
    items = list(ingest(str(sample_video), start_time=T0))
    kinds = {type(i) for i in items}
    assert kinds == {Keyframe, AudioChunk}
    stamps = [i.timestamp for i in items]
    assert stamps == sorted(stamps)
    assert sum(isinstance(i, Keyframe) for i in items) in range(5, 8)
    assert sum(isinstance(i, AudioChunk) for i in items) == 2


def test_ac_per_01_start_time_offsets_everything(sample_video):
    later = T0 + timedelta(hours=3)
    frames = list(iter_keyframes(str(sample_video), start_time=later))
    assert frames[0].timestamp >= later
    assert frames[0].timestamp - later < timedelta(seconds=1)
