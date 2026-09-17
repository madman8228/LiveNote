from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from llm import LlmError, generate_summary, is_configured
except ImportError:
    from .llm import LlmError, generate_summary, is_configured


class ContentError(RuntimeError):
    pass


def load_transcript(data_dir: Path, session_id: str) -> dict[str, Any]:
    path = data_dir / 'processed' / 'sessions' / session_id / 'transcript.json'
    if not path.is_file():
        raise ContentError('该 Session 尚未生成逐字稿。')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:
        raise ContentError(f'逐字稿读取失败：{error}') from error
    if not isinstance(value, dict):
        raise ContentError('逐字稿格式无效。')
    return value


def build_structured_report(session: dict[str, Any], markers: list[dict[str, Any]], transcript: dict[str, Any], generated_at: int) -> dict[str, Any]:
    groups = {
        'KEY_POINT': 'keyPoints',
        'QUESTION': 'questions',
        'IDEA': 'ideas',
        'TODO': 'todos',
    }
    marker_sections = {name: [] for name in groups.values()}
    for marker in sorted(markers, key=lambda item: (item.get('elapsedMs', 0), item.get('createdAt', 0))):
        section = groups.get(str(marker.get('type', '')))
        if section:
            marker_sections[section].append({
                'id': marker.get('id'),
                'elapsedMs': marker.get('elapsedMs', 0),
                'wallClockMs': marker.get('wallClockMs', 0),
                'note': marker.get('note', ''),
                'createdAt': marker.get('createdAt', 0),
            })

    segments = transcript.get('segments', [])
    local_draft = {
        'overviewPreview': str(transcript.get('text', '')).strip()[:300],
        'keyPoints': marker_sections['keyPoints'],
        'questions': marker_sections['questions'],
        'ideas': marker_sections['ideas'],
        'todos': marker_sections['todos'],
    }
    summary = None
    summary_status = 'NOT_CONFIGURED'
    summary_error = None
    if is_configured():
        try:
            summary = generate_summary(session, transcript, markers)
            summary_status = 'GENERATED' if summary else 'NOT_GENERATED'
        except LlmError as error:
            summary_status = 'FAILED'
            summary_error = str(error)
    return {
        'version': 1,
        'processingMode': 'structured-draft-with-optional-llm',
        'summaryStatus': summary_status,
        'summaryError': summary_error,
        'summary': summary,
        'localDraft': local_draft,
        'analysisProvider': 'llm' if summary else 'local-marker-draft',
        'generatedAt': generated_at,
        'session': {
            'id': session['id'],
            'title': session['title'],
            'startedAt': session['started_at'],
            'endedAt': session['ended_at'],
            'status': session['status'],
            'durationMs': session['duration_ms'],
        },
        'transcript': {
            'model': transcript.get('model'),
            'device': transcript.get('device'),
            'language': transcript.get('language'),
            'audioQuality': transcript.get('audioQuality'),
            'text': transcript.get('text', ''),
            'segments': segments,
            'chunked': bool(transcript.get('chunked', False)),
            'chunkDurationSeconds': transcript.get('chunkDurationSeconds'),
            'chunkOverlapSeconds': transcript.get('chunkOverlapSeconds'),
        },
        'timeline': segments,
        'markers': marker_sections,
        'counts': {
            'transcriptSegments': len(segments),
            'markers': len(markers),
            **{name: len(items) for name, items in marker_sections.items()},
        },
    }


def save_report(data_dir: Path, session_id: str, report: dict[str, Any]) -> Path:
    output_path = data_dir / 'processed' / 'sessions' / session_id / 'report.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f'.{output_path.name}.tmp')
    temporary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary_path.replace(output_path)
    return output_path
