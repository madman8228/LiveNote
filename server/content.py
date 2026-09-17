from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from llm import LlmError, generate_summary, is_configured
except ImportError:
    from .llm import LlmError, generate_summary, is_configured


class ContentError(RuntimeError):
    pass


def _extractive_draft(segments: list[Any], transcript_text: str, generated_at: int) -> dict[str, Any]:
    """Build an honest, deterministic fallback when no LLM is configured.

    This is intentionally extractive: it only reuses ASR text and never
    invents a conclusion. The report keeps summaryStatus=NOT_CONFIGURED so
    callers can distinguish it from semantic model output.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_segments = segments if segments else [{'text': part} for part in re.split(r'(?<=[。！？!?；;])\s*|[\r\n]+', transcript_text)]
    keywords = ('重点', '核心', '结论', '原因', '方法', '步骤', '因为', '所以', '需要', '可以', '注意', '问题', '解决', '首先', '其次', '总结')

    for source in source_segments:
        if not isinstance(source, dict):
            continue
        text = re.sub(r'\s+', ' ', str(source.get('text', '')).strip())
        compact = re.sub(r'\s+', '', text)
        if len(compact) < 6 or compact in seen:
            continue
        seen.add(compact)
        score = min(len(compact), 80) + sum(12 for keyword in keywords if keyword in compact)
        candidates.append({
            'text': text,
            'score': score,
            'order': len(candidates),
            'elapsedMs': source.get('startMs') if isinstance(source.get('startMs'), int) else 0,
        })

    candidates.sort(key=lambda item: (-item['score'], item['order']))
    selected = sorted(candidates[:8], key=lambda item: item['order'])
    key_points = [
        {
            'id': f'local-key-point-{index}',
            'elapsedMs': item['elapsedMs'],
            'wallClockMs': 0,
            'note': item['text'],
            'createdAt': generated_at,
        }
        for index, item in enumerate(selected)
    ]
    overview_parts = []
    for item in sorted(candidates, key=lambda item: item['order']):
        if item['text'] not in overview_parts:
            overview_parts.append(item['text'])
        if len(' '.join(overview_parts)) >= 300:
            break
    overview = ' '.join(overview_parts)[:300] or transcript_text.strip()[:300]
    return {
        'overviewPreview': overview,
        'keyPoints': key_points,
        'knowledgeStructure': [{
            'title': '本地抽取式要点',
            'points': [item['note'] for item in key_points],
        }] if key_points else [],
    }


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
    extractive_draft = _extractive_draft(segments if isinstance(segments, list) else [], str(transcript.get('text', '')), generated_at)
    local_draft = {
        **extractive_draft,
        'source': 'extractive',
        'keyPoints': marker_sections['keyPoints'] + extractive_draft['keyPoints'],
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
        'analysisProvider': 'llm' if summary else 'local-extractive-draft',
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
