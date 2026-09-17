from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any


class AsrError(RuntimeError):
    pass


SUPPORTED_MODELS = {'tiny', 'base', 'small', 'medium', 'large-v1', 'large-v2', 'large-v3', 'large', 'turbo', 'large-v3-turbo'}
DEFAULT_MODEL = os.environ.get('LIVENOTE_WHISPER_MODEL', 'medium')
MODEL_CACHE = Path(os.environ.get('LIVENOTE_WHISPER_CACHE', Path.home() / '.cache' / 'whisper'))
FFMPEG = os.environ.get('LIVENOTE_FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('LIVENOTE_FFPROBE', 'ffprobe')
MIN_MEAN_VOLUME_DB = float(os.environ.get('LIVENOTE_ASR_MIN_MEAN_DB', '-48'))
MIN_MAX_VOLUME_DB = float(os.environ.get('LIVENOTE_ASR_MIN_MAX_DB', '-32'))
ASR_CHUNK_SECONDS = max(60, int(os.environ.get('LIVENOTE_ASR_CHUNK_SECONDS', '300')))
ASR_CHUNK_OVERLAP_SECONDS = max(0, min(10, int(os.environ.get('LIVENOTE_ASR_CHUNK_OVERLAP_SECONDS', '1'))))


def _measure_audio_volume(audio_path: Path) -> dict[str, float | None]:
    try:
        result = subprocess.run(
            [FFMPEG, '-hide_banner', '-i', str(audio_path), '-af', 'volumedetect', '-f', 'null', '-'],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {'meanVolumeDb': None, 'maxVolumeDb': None}

    output = f'{result.stdout}\n{result.stderr}'

    def value(name: str) -> float | None:
        match = re.search(rf'{name}:\s*(-?\d+(?:\.\d+)?)\s*dB', output)
        return float(match.group(1)) if match else None

    return {'meanVolumeDb': value('mean_volume'), 'maxVolumeDb': value('max_volume')}


def _validate_audio_input(audio_path: Path) -> dict[str, float | None]:
    volume = _measure_audio_volume(audio_path)
    mean_volume = volume['meanVolumeDb']
    max_volume = volume['maxVolumeDb']
    if (mean_volume is not None and mean_volume < MIN_MEAN_VOLUME_DB) or (max_volume is not None and max_volume < MIN_MAX_VOLUME_DB):
        details = []
        if mean_volume is not None:
            details.append(f'平均音量 {mean_volume:.1f} dB')
        if max_volume is not None:
            details.append(f'峰值 {max_volume:.1f} dB')
        raise AsrError(
            f'录音输入音量过低（{"，".join(details)}）。请使用 Speech 配置，提高手机 A 播放音量并重新录音。'
        )
    return volume


def _prepare_audio_for_asr(audio_path: Path, start_seconds: float = 0, duration_seconds: float | None = None) -> Path:
    temporary_file = tempfile.NamedTemporaryFile(prefix='livenote-asr-', suffix='.wav', delete=False)
    prepared_path = Path(temporary_file.name)
    temporary_file.close()
    command = [FFMPEG, '-y', '-v', 'error', '-i', str(audio_path)]
    if start_seconds > 0:
        command.extend(['-ss', f'{start_seconds:.3f}'])
    command.extend([
        '-vn',
        '-ac',
        '1',
        '-ar',
        '16000',
        '-af',
        'highpass=f=80,lowpass=f=7600,loudnorm=I=-16:TP=-1.5:LRA=11',
        '-c:a',
        'pcm_s16le',
        str(prepared_path),
    ])
    if duration_seconds is not None:
        command[-1:-1] = ['-t', f'{duration_seconds:.3f}']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError as error:
        prepared_path.unlink(missing_ok=True)
        raise AsrError(f'未找到 FFmpeg：{FFMPEG}。请安装 FFmpeg 或设置 LIVENOTE_FFMPEG。') from error
    except subprocess.TimeoutExpired as error:
        prepared_path.unlink(missing_ok=True)
        raise AsrError('ASR 音频预处理超时。') from error
    if result.returncode != 0 or not prepared_path.is_file() or prepared_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or '').strip()
        prepared_path.unlink(missing_ok=True)
        raise AsrError(f'ASR 音频预处理失败：{detail[-1000:]}')
    return prepared_path


def _probe_duration_seconds(audio_path: Path) -> float:
    try:
        result = subprocess.run(
            [FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(audio_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as error:
        raise AsrError(f'未找到 FFprobe：{FFPROBE}。请安装 FFmpeg 或设置 LIVENOTE_FFPROBE。') from error
    except subprocess.TimeoutExpired as error:
        raise AsrError('ASR 音频时长检测超时。') from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise AsrError(f'ASR 音频时长检测失败：{detail[-500:]}')
    try:
        duration = float(result.stdout.strip())
    except ValueError as error:
        raise AsrError('无法读取 ASR 音频时长。') from error
    if duration <= 0:
        raise AsrError('ASR 音频时长无效。')
    return duration


def _model_file(model_name: str) -> Path:
    filename = 'large-v3-turbo.pt' if model_name in {'turbo', 'large-v3-turbo'} else f'{model_name}.pt'
    return MODEL_CACHE / filename


def is_model_cached(model_name: str = DEFAULT_MODEL) -> bool:
    return model_name in SUPPORTED_MODELS and _model_file(model_name).is_file()


@lru_cache(maxsize=2)
def _load_model(model_name: str) -> tuple[Any, str]:
    if model_name not in SUPPORTED_MODELS:
        raise AsrError(f'不支持的 Whisper 模型：{model_name}')
    if not _model_file(model_name).is_file():
        raise AsrError(f'本机没有缓存 Whisper 模型 {model_name}：{_model_file(model_name)}。请先准备模型文件。')
    try:
        import torch
        import whisper
    except ImportError as error:
        raise AsrError('未安装 openai-whisper 或 torch。') from error

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    try:
        return whisper.load_model(model_name, device=device, download_root=str(MODEL_CACHE)), device
    except Exception as error:
        raise AsrError(f'加载 Whisper 模型失败：{error}') from error


def _transcribe_prepared(model: Any, device: str, prepared_path: Path, language: str) -> dict[str, Any]:
    try:
        result = model.transcribe(
            str(prepared_path),
            language=language,
            task='transcribe',
            fp16=device == 'cuda',
            # Let Whisper retry low-confidence/over-compressed segments with
            # higher decoding temperatures. A fixed 0.0 decode is faster, but
            # it makes short Chinese words particularly prone to one-shot
            # homophone errors (for example 录音 -> 路音).
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            beam_size=5,
            verbose=False,
            condition_on_previous_text=False,
        )
    except Exception as error:
        raise AsrError(f'Whisper 转写失败：{error}') from error
    return result


def _normalise_segments(result: dict[str, Any], offset_ms: int = 0, trim_before_ms: int = 0) -> list[dict[str, Any]]:
    segments = []
    for segment in result.get('segments', []):
        text = str(segment.get('text', '')).strip()
        if not text:
            continue
        local_start_ms = max(0, round(float(segment.get('start', 0)) * 1000))
        local_end_ms = max(local_start_ms, round(float(segment.get('end', 0)) * 1000))
        if local_end_ms <= trim_before_ms:
            continue
        segments.append({
            'startMs': offset_ms + max(local_start_ms, trim_before_ms),
            'endMs': offset_ms + local_end_ms,
            'text': text,
        })
    return segments


def _transcribe_long_audio(audio_path: Path, model: Any, device: str, language: str, duration_seconds: float) -> tuple[str, list[dict[str, Any]]]:
    step_seconds = max(1, ASR_CHUNK_SECONDS - ASR_CHUNK_OVERLAP_SECONDS)
    all_segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    start_seconds = 0.0
    chunk_index = 0
    while start_seconds < duration_seconds:
        chunk_duration = min(ASR_CHUNK_SECONDS, duration_seconds - start_seconds)
        prepared_path = _prepare_audio_for_asr(audio_path, start_seconds, chunk_duration)
        try:
            result = _transcribe_prepared(model, device, prepared_path, language)
        finally:
            prepared_path.unlink(missing_ok=True)

        trim_before_ms = round(ASR_CHUNK_OVERLAP_SECONDS * 1000) if chunk_index > 0 else 0
        offset_ms = round(start_seconds * 1000)
        chunk_segments = _normalise_segments(result, offset_ms, trim_before_ms)
        all_segments.extend(chunk_segments)
        text_parts.extend(str(segment.get('text', '')).strip() for segment in chunk_segments if str(segment.get('text', '')).strip())
        if start_seconds + chunk_duration >= duration_seconds:
            break
        start_seconds += step_seconds
        chunk_index += 1

    all_segments.sort(key=lambda segment: (segment['startMs'], segment['endMs']))
    return ' '.join(text_parts).strip(), all_segments


def transcribe(audio_path: Path, model_name: str = DEFAULT_MODEL, language: str = 'zh') -> dict[str, Any]:
    if not audio_path.is_file():
        raise AsrError(f'音频文件不存在：{audio_path}')
    audio_quality = _validate_audio_input(audio_path)
    model, device = _load_model(model_name)
    duration_seconds = _probe_duration_seconds(audio_path)

    if duration_seconds > ASR_CHUNK_SECONDS:
        text, segments = _transcribe_long_audio(audio_path, model, device, language, duration_seconds)
        return {
            'model': model_name,
            'device': device,
            'language': language,
            'audioQuality': audio_quality,
            'text': text,
            'segments': [{**segment, 'index': index} for index, segment in enumerate(segments)],
            'chunked': True,
            'chunkDurationSeconds': ASR_CHUNK_SECONDS,
            'chunkOverlapSeconds': ASR_CHUNK_OVERLAP_SECONDS,
        }

    prepared_path = _prepare_audio_for_asr(audio_path)
    try:
        result = _transcribe_prepared(model, device, prepared_path, language)
    finally:
        prepared_path.unlink(missing_ok=True)

    normalised_segments = _normalise_segments(result)
    segments = [{**segment, 'index': index} for index, segment in enumerate(normalised_segments)]
    return {
        'model': model_name,
        'device': device,
        'language': language,
        'audioQuality': audio_quality,
        # Keep the plain transcript consistent with the timeline after empty
        # or rejected segments have been removed. Using result['text'] here
        # could reintroduce hallucinated text that is absent from segments.
        'text': ' '.join(segment['text'] for segment in normalised_segments).strip(),
        'segments': segments,
        'chunked': False,
    }


def save_transcript(data_dir: Path, session_id: str, transcript: dict[str, Any]) -> Path:
    output_path = data_dir / 'processed' / 'sessions' / session_id / 'transcript.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f'.{output_path.name}.tmp')
    temporary_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary_path.replace(output_path)
    return output_path
