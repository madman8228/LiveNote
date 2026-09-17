from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ReconstructionError(RuntimeError):
    pass


FFMPEG = os.environ.get('LIVENOTE_FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('LIVENOTE_FFPROBE', 'ffprobe')


def _temporary_output_path(output_path: Path) -> Path:
    # FFmpeg chooses the muxer from the output suffix. Keep .webm as the
    # final suffix instead of producing names such as `.session.webm.tmp`.
    return output_path.with_name(f'.{output_path.stem}.tmp{output_path.suffix}')


def _run(command: list[str], timeout: int = 300) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ReconstructionError(f'未找到 FFmpeg：{command[0]}。请安装 FFmpeg 或设置对应环境变量。') from error
    except subprocess.TimeoutExpired as error:
        raise ReconstructionError('音频重建超时。') from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise ReconstructionError(f'FFmpeg 音频重建失败：{detail[-1000:]}')


def _validate_media(path: Path) -> None:
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=format_name,duration,size', '-of', 'default=nw=1', str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as error:
        raise ReconstructionError(f'未找到 FFprobe：{FFPROBE}。请安装 FFmpeg 或设置对应环境变量。') from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise ReconstructionError(f'重建后的音频无法验证：{detail[-1000:]}')


def _write_chunk_sequence(chunk_paths: list[Path], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('wb') as output:
        for chunk_path in chunk_paths:
            if not chunk_path.is_file():
                raise ReconstructionError(f'Chunk 文件不存在：{chunk_path}')
            with chunk_path.open('rb') as chunk:
                shutil.copyfileobj(chunk, output, length=1024 * 1024)


def _concat_segments(segment_paths: list[Path], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.txt', dir=output_path.parent, delete=False) as list_file:
        list_path = Path(list_file.name)
        for segment_path in segment_paths:
            # concat demuxer accepts single-quoted paths with doubled quotes.
            escaped = str(segment_path.resolve()).replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")

    temporary_output = _temporary_output_path(output_path)
    try:
        # A stream-copy concat of independent MediaRecorder WebM files can
        # keep only the first initialization/timeline in some FFmpeg builds.
        # Re-encode the audio stream so every Segment contributes to one
        # continuous, correctly timed output file.
        _run([
            FFMPEG, '-y', '-v', 'error',
            '-f', 'concat', '-safe', '0', '-i', str(list_path),
            '-map', '0:a:0', '-c:a', 'libopus', '-b:a', '64k',
            '-application', 'audio', str(temporary_output),
        ])
        _validate_media(temporary_output)
        temporary_output.replace(output_path)
    finally:
        list_path.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)


def reconstruct_segment(
    data_dir: Path,
    session_id: str,
    segment_index: int,
    chunk_paths: list[Path],
) -> Path:
    if not chunk_paths:
        raise ReconstructionError('Segment 没有可重建的 Chunk。')

    output_path = data_dir / 'reconstructed' / 'sessions' / session_id / f'segment_{segment_index}.webm'
    temporary_path = _temporary_output_path(output_path)
    _write_chunk_sequence(chunk_paths, temporary_path)
    try:
        _validate_media(temporary_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def reconstruct_session(data_dir: Path, session_id: str, segment_paths: list[tuple[int, Path]]) -> Path:
    if not segment_paths:
        raise ReconstructionError('Session 没有可重建的 Segment。')

    ordered = sorted(segment_paths, key=lambda item: item[0])
    indexes = [index for index, _ in ordered]
    if indexes != list(range(1, len(indexes) + 1)):
        raise ReconstructionError(f'Segment index 不连续：{indexes}')

    output_path = data_dir / 'reconstructed' / 'sessions' / session_id / 'session.webm'
    if len(ordered) == 1:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = _temporary_output_path(output_path)
        try:
            # A byte-concatenated MediaRecorder WebM is playable, but usually
            # has no container duration. Remux the single-segment case so the
            # native browser player receives a stable duration/time axis.
            _run([FFMPEG, '-y', '-v', 'error', '-i', str(ordered[0][1]), '-c', 'copy', str(temporary_output)])
            _validate_media(temporary_output)
            temporary_output.replace(output_path)
        finally:
            temporary_output.unlink(missing_ok=True)
    else:
        _concat_segments([path for _, path in ordered], output_path)
    return output_path
