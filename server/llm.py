from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


class LlmError(RuntimeError):
    pass


LLM_BASE_URL = os.environ.get('LIVENOTE_LLM_BASE_URL', '').rstrip('/')
LLM_API_KEY = os.environ.get('LIVENOTE_LLM_API_KEY', '')
LLM_MODEL = os.environ.get('LIVENOTE_LLM_MODEL', '')
LLM_TIMEOUT_SECONDS = int(os.environ.get('LIVENOTE_LLM_TIMEOUT_SECONDS', '180'))
LLM_MAX_TRANSCRIPT_CHARS = int(os.environ.get('LIVENOTE_LLM_MAX_TRANSCRIPT_CHARS', '120000'))
LLM_MAX_TIMELINE_SEGMENTS = int(os.environ.get('LIVENOTE_LLM_MAX_TIMELINE_SEGMENTS', '1000'))
LLM_SUMMARY_CHUNK_CHARS = int(os.environ.get('LIVENOTE_LLM_SUMMARY_CHUNK_CHARS', '50000'))


def is_configured() -> bool:
    # Local OpenAI-compatible servers such as Ollama commonly do not require
    # an API key. Remote providers can still require one and will return a
    # clear HTTP error if it is missing.
    return bool(LLM_BASE_URL and LLM_MODEL)


def _extract_json(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        object_start = cleaned.find('{')
        if object_start < 0:
            raise LlmError('模型返回的总结不是有效 JSON。') from error
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[object_start:])
        except json.JSONDecodeError as nested_error:
            raise LlmError('模型返回的总结不是有效 JSON。') from nested_error
    if not isinstance(value, dict):
        raise LlmError('模型返回的总结格式无效。')
    return value


def _normalise_summary(value: dict[str, Any]) -> dict[str, Any]:
    def strings(name: str) -> list[str]:
        raw = value.get(name, [])
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    questions = value.get('questions', [])
    if not isinstance(questions, list):
        questions = []
    normalised_questions = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        normalised_questions.append({
            'question': str(item.get('question', '')).strip(),
            'answer': str(item.get('answer', '')).strip(),
            'startMs': item.get('startMs') if isinstance(item.get('startMs'), int) else None,
        })

    knowledge_structure = []
    raw_structure = value.get('knowledgeStructure', [])
    if isinstance(raw_structure, list):
        for item in raw_structure:
            if isinstance(item, dict):
                title = str(item.get('title', '')).strip()
                points = item.get('points', [])
                if not isinstance(points, list):
                    points = []
                points = [str(point).strip() for point in points if str(point).strip()]
                if title or points:
                    knowledge_structure.append({'title': title, 'points': points})
            elif str(item).strip():
                knowledge_structure.append({'title': str(item).strip(), 'points': []})

    return {
        'title': str(value.get('title', '')).strip(),
        'overview': str(value.get('overview', '')).strip(),
        'keyPoints': strings('keyPoints'),
        'knowledgeStructure': knowledge_structure,
        'questions': normalised_questions,
        'actionItems': strings('actionItems'),
        'entities': strings('entities'),
        'confidenceNotes': strings('confidenceNotes'),
    }


def _marker_payload(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'type': marker.get('type'),
            'elapsedMs': marker.get('elapsedMs', 0),
            'note': marker.get('note', ''),
        }
        for marker in markers
    ]


def _summary_system(scope: str = 'full') -> str:
    if scope == 'partial':
        return (
            '你是中文直播内容分析器。以下是长直播的一个局部片段，只总结该片段中明确出现的内容，'
            '不要把局部内容夸大为整场结论，不要补写逐字稿中没有的事实。'
            '必须只返回 JSON，不要 Markdown。字段必须包括：title、overview、keyPoints、knowledgeStructure、questions、actionItems、entities、confidenceNotes。'
            'questions 是对象数组，每项包括 question、answer、startMs；无法确认时使用空字符串或 null。'
            '所有总结应尽量保留原文含义，避免夸大和臆测。'
        )
    return (
        '你是中文直播内容分析器。只根据提供的逐字稿和用户标记整理内容，不得补写逐字稿中没有的事实。'
        '必须只返回 JSON，不要 Markdown。字段必须包括：title、overview、keyPoints、knowledgeStructure、questions、actionItems、entities、confidenceNotes。'
        'questions 是对象数组，每项包括 question、answer、startMs；无法确认时使用空字符串或 null。'
        '所有总结应尽量保留原文含义，避免夸大和臆测。'
    )


def _request_summary(system: str, prompt: dict[str, Any]) -> dict[str, Any]:
    request_body = json.dumps({
        'model': LLM_MODEL,
        'temperature': 0.2,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode('utf-8')
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    if LLM_API_KEY:
        headers['Authorization'] = f'Bearer {LLM_API_KEY}'
    request = urllib.request.Request(
        f'{LLM_BASE_URL}/chat/completions',
        data=request_body,
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[-500:]
        raise LlmError(f'内容总结服务请求失败（HTTP {error.code}）：{detail}') from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise LlmError(f'内容总结服务不可用：{error}') from error
    except json.JSONDecodeError as error:
        raise LlmError('内容总结服务返回了无效响应。') from error

    try:
        content = payload['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as error:
        raise LlmError('内容总结服务响应缺少模型内容。') from error
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
            elif isinstance(item, str):
                parts.append(item)
        content = ''.join(parts)
    if not isinstance(content, str):
        raise LlmError('内容总结服务返回的模型内容格式无效。')
    return _normalise_summary(_extract_json(content))


def _chunk_transcript(segments: list[Any], transcript_text: str, max_chars: int) -> list[dict[str, Any]]:
    if not segments:
        return [{'transcript': transcript_text[index:index + max_chars], 'timeline': []} for index in range(0, len(transcript_text), max_chars)]

    chunks: list[dict[str, Any]] = []
    current_segments: list[Any] = []
    current_text: list[str] = []
    current_length = 0
    for segment in segments:
        text = str(segment.get('text', '')).strip() if isinstance(segment, dict) else str(segment).strip()
        if not text:
            continue
        if len(text) > max_chars:
            if current_segments:
                chunks.append({'transcript': '\n'.join(current_text), 'timeline': current_segments})
                current_segments = []
                current_text = []
                current_length = 0
            for index in range(0, len(text), max_chars):
                part = text[index:index + max_chars]
                chunks.append({'transcript': part, 'timeline': [{**segment, 'text': part}] if isinstance(segment, dict) else []})
            continue
        if current_segments and current_length + len(text) + 1 > max_chars:
            chunks.append({'transcript': '\n'.join(current_text), 'timeline': current_segments})
            current_segments = []
            current_text = []
            current_length = 0
        current_segments.append(segment)
        current_text.append(text)
        current_length += len(text) + 1
    if current_segments:
        chunks.append({'transcript': '\n'.join(current_text), 'timeline': current_segments})
    return chunks or [{'transcript': transcript_text[:max_chars], 'timeline': []}]


def generate_summary(session: dict[str, Any], transcript: dict[str, Any], markers: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not is_configured():
        return None

    transcript_text = str(transcript.get('text', '')).strip()
    if not transcript_text:
        raise LlmError('ASR 没有输出文本，无法生成内容总结。')
    segments = transcript.get('segments', [])
    segments = segments if isinstance(segments, list) else []
    marker_text = _marker_payload(markers)

    if len(transcript_text) <= LLM_MAX_TRANSCRIPT_CHARS and len(segments) <= LLM_MAX_TIMELINE_SEGMENTS:
        return _request_summary(_summary_system(), {
            'sessionTitle': session.get('title', ''),
            'transcript': transcript_text,
            'timeline': segments,
            'userMarkers': marker_text,
        })

    partials: list[dict[str, Any]] = []
    for index, part in enumerate(_chunk_transcript(segments, transcript_text, LLM_SUMMARY_CHUNK_CHARS), start=1):
        partials.append(_request_summary(_summary_system('partial'), {
            'sessionTitle': session.get('title', ''),
            'partIndex': index,
            'transcript': part['transcript'],
            'timeline': part['timeline'][:LLM_MAX_TIMELINE_SEGMENTS],
            'userMarkers': marker_text,
        }))

    merge_system = (
        '你是中文直播内容总编辑。请把多个局部总结合并成一份整场直播总结。'
        '只能使用局部总结和用户标记中明确出现的信息，不得补写事实。'
        '去除重复内容，保留重要知识点之间的层级关系；无法确认的问题不要编造答案。'
        '必须只返回 JSON，不要 Markdown。字段必须包括：title、overview、keyPoints、knowledgeStructure、questions、actionItems、entities、confidenceNotes。'
        'questions 是对象数组，每项包括 question、answer、startMs；无法确认时使用空字符串或 null。'
    )
    return _request_summary(merge_system, {
        'sessionTitle': session.get('title', ''),
        'partialSummaries': partials,
        'userMarkers': marker_text,
    })
