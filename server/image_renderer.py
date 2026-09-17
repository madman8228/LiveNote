from __future__ import annotations

import html
import textwrap
from typing import Any


WIDTH = 1200


def _lines(value: Any, width: int = 34, limit: int = 6) -> list[str]:
    text = str(value or '').strip()
    if not text:
        return []
    return textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)[:limit]


def _text(text: str, x: int, y: int, size: int, color: str = '#dce9e9', weight: int = 400) -> str:
    return f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}px" font-weight="{weight}" font-family="Microsoft YaHei,Noto Sans SC,sans-serif">{html.escape(text)}</text>'


def render_summary_svg(report: dict[str, Any]) -> str:
    summary = report.get('summary') or {}
    local_draft = report.get('localDraft') or {}
    session = report.get('session') or {}
    title = summary.get('title') or session.get('title') or 'LiveNote 总结'
    overview = summary.get('overview') or local_draft.get('overviewPreview') or '暂无内容概览。'
    key_points = summary.get('keyPoints') or [item.get('note', '') for item in local_draft.get('keyPoints', [])]
    action_items = summary.get('actionItems') or [item.get('note', '') for item in local_draft.get('todos', [])]
    questions = summary.get('questions') or [
        {'question': item.get('note', ''), 'answer': '', 'startMs': item.get('elapsedMs')}
        for item in local_draft.get('questions', [])
    ]

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" viewBox="0 0 {WIDTH} 1800">',
        '<rect width="1200" height="1800" fill="#07131f"/>',
        '<rect x="56" y="56" width="1088" height="1688" rx="28" fill="#0d2230" stroke="#294452" stroke-width="2"/>',
        _text('LIVENOTE / CONTENT SUMMARY', 100, 132, 22, '#c9e081', 700),
        _text(str(title)[:60], 100, 205, 48, '#f0f6f5', 700),
        _text(f"状态：{report.get('summaryStatus', 'NOT_CONFIGURED')} · 来源：{report.get('analysisProvider', 'local')}", 100, 252, 20, '#9db4b8'),
    ]
    y = 330

    def section(label: str, items: list[str], accent: str = '#c9e081') -> None:
        nonlocal y
        elements.append(_text(label, 100, y, 28, accent, 700))
        y += 46
        for item in items:
            for line in _lines(item):
                elements.append(_text(line, 124, y, 25, '#dce9e9'))
                y += 36
            y += 8
        y += 28

    section('内容概览', _lines(overview, width=42, limit=8))
    section('关键知识点', [str(item) for item in key_points if str(item).strip()] or ['暂无标记或 AI 提取的关键知识点。'])
    question_lines = []
    for item in questions[:8]:
        question = str(item.get('question', '')).strip()
        answer = str(item.get('answer', '')).strip()
        if question:
            question_lines.append(f'问：{question}')
        if answer:
            question_lines.append(f'答：{answer}')
    section('问答', question_lines or ['暂无问答内容。'])
    section('待办事项', [str(item) for item in action_items if str(item).strip()] or ['暂无待办事项。'])

    elements.append(_text('说话人识别：尚未接入', 100, min(y, 1660), 20, '#9db4b8'))
    elements.append(_text('LiveNote · 本图片由结构化内容自动排版生成', 100, 1710, 18, '#6f8b91'))
    elements.append('</svg>')
    return ''.join(elements)
