from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_report(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise ValueError('诊断文件必须是 JSON 对象。')
    return report


def _events(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in [*report.get('preparation', []), *report.get('events', [])] if isinstance(item, dict)]
    return sorted(items, key=lambda item: (float(item.get('atMs', 0) or 0), int(item.get('seq', 0) or 0)))


def analyze(report: dict[str, Any]) -> dict[str, Any]:
    events = _events(report)
    samples = [event for event in events if event.get('type') in {'media-sample', 'timeupdate'} and isinstance(event.get('data'), dict)]
    duration_values = [event['data'].get('duration') for event in samples if isinstance(event['data'].get('duration'), (int, float))]
    position_rollbacks: list[dict[str, Any]] = []
    duration_changes: list[dict[str, Any]] = []
    seeking_times = [float(event.get('atMs', 0) or 0) for event in events if event.get('type') in {'seeking', 'seeked'}]
    previous_position: tuple[float, float] | None = None
    previous_duration: float | None = None
    for event in samples:
        data = event['data']
        at_ms = float(event.get('atMs', 0) or 0)
        current = data.get('currentTime')
        duration = data.get('duration')
        if isinstance(current, (int, float)):
            if previous_position is not None:
                previous_at, previous_current = previous_position
                delta = float(current) - previous_current
                seek_nearby = any(abs(at_ms - seek_time) <= 1_000 for seek_time in seeking_times)
                if delta < -0.25 and not seek_nearby:
                    position_rollbacks.append({'atMs': at_ms, 'from': previous_current, 'to': float(current), 'delta': delta})
            previous_position = (at_ms, float(current))
        if isinstance(duration, (int, float)):
            if previous_duration is not None and abs(float(duration) - previous_duration) > 0.25:
                duration_changes.append({'atMs': at_ms, 'from': previous_duration, 'to': float(duration)})
            previous_duration = float(duration)

    source_changes = [event for event in events if event.get('type') in {'source-assigned', 'source-revoked'}]
    element_changes = [event for event in events if event.get('type') in {'audio-mount', 'audio-unmount'}]
    buffer_gaps: list[dict[str, Any]] = []
    for event in samples:
        buffered = event.get('data', {}).get('buffered', [])
        if isinstance(buffered, list) and len(buffered) > 1:
            buffer_gaps.append({'atMs': event.get('atMs'), 'ranges': buffered})

    candidates: list[str] = []
    missing: list[str] = []
    if position_rollbacks:
        candidates.append('播放位置出现未伴随 seek 事件的明显回退。')
    if duration_changes:
        candidates.append('媒体 duration 在播放过程中发生变化，可能导致进度比例跳动。')
    if len(source_changes) > 1:
        candidates.append('播放源发生多次分配或撤销，可能导致播放器重建。')
    if len(element_changes) > 1:
        candidates.append('audio 元素发生多次挂载/卸载，可能导致播放状态重置。')
    if buffer_gaps:
        candidates.append('采样中出现多个 buffered 范围，可能存在缓冲缺口。')
    if not samples:
        missing.append('没有采集到播放位置采样，无法判断 currentTime 是否回退。')
    if report.get('droppedEvents', 0):
        missing.append(f"事件缓冲丢弃了 {report['droppedEvents']} 条记录，时间线可能不完整。")
    if not any(event.get('type') in {'play', 'playing'} for event in events):
        missing.append('没有采集到 play/playing，无法确认用户是否真正开始播放。')
    return {
        'identity': {key: report.get(key) for key in ('kind', 'schemaVersion', 'clientReportId', 'sessionId', 'attemptId', 'buildId')},
        'coverage': {'eventCount': len(report.get('events', [])), 'preparationEventCount': len(report.get('preparation', [])), 'sampleCount': len(samples), 'droppedEvents': report.get('droppedEvents', 0)},
        'evidence': {'positionRollbacks': position_rollbacks, 'durationChanges': duration_changes, 'sourceChanges': len(source_changes), 'elementChanges': len(element_changes), 'bufferGapSamples': len(buffer_gaps), 'durationValues': duration_values[:20]},
        'candidates': candidates or ['当前包没有命中明显的时间轴异常。'],
        'missingEvidence': missing or ['无明显证据缺口。'],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='分析单个 LiveNote 播放诊断包。')
    parser.add_argument('--input', required=True, type=Path, help='明确指定的 snapshot.json 路径')
    args = parser.parse_args()
    try:
        report = load_report(args.input)
        print(json.dumps(analyze(report), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f'分析失败：{error}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
