import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from server import asr


class FakeWhisperModel:
    def __init__(self) -> None:
        self.calls = 0
        self.options = []

    def transcribe(self, _path: str, **kwargs):
        self.calls += 1
        self.options.append(kwargs)
        return {
            'text': f'第{self.calls}段',
            'segments': [
                {'start': 0.0, 'end': 1.5, 'text': f'第{self.calls}段'},
                {'start': 2.0, 'end': 3.0, 'text': f'后半段{self.calls}'},
            ],
        }


class AsrTests(unittest.TestCase):
    def test_long_audio_uses_multiple_chunks_and_offsets_timestamps(self) -> None:
        model = FakeWhisperModel()

        def fake_prepare(_audio: Path, _start: float = 0, _duration: float | None = None) -> Path:
            descriptor, name = tempfile.mkstemp(suffix='.wav')
            os.close(descriptor)
            file = Path(name)
            file.write_bytes(b'wav')
            return file

        with tempfile.NamedTemporaryFile(suffix='.webm') as source, patch.object(asr, '_validate_audio_input', return_value={'meanVolumeDb': -20.0, 'maxVolumeDb': -3.0}), patch.object(asr, '_load_model', return_value=(model, 'cpu')), patch.object(asr, '_probe_duration_seconds', return_value=601.0), patch.object(asr, '_prepare_audio_for_asr', side_effect=fake_prepare):
            result = asr.transcribe(Path(source.name), model_name='tiny')

        self.assertTrue(result['chunked'])
        self.assertEqual(result['chunkDurationSeconds'], asr.ASR_CHUNK_SECONDS)
        self.assertGreater(model.calls, 1)
        self.assertEqual(result['segments'][0]['startMs'], 0)
        self.assertEqual(result['segments'], sorted(result['segments'], key=lambda item: item['startMs']))
        self.assertTrue(all(item['endMs'] >= item['startMs'] for item in result['segments']))
        self.assertEqual(model.options[0]['temperature'], (0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
        self.assertFalse(model.options[0]['condition_on_previous_text'])

    def test_short_transcript_matches_normalised_timeline(self) -> None:
        model = FakeWhisperModel()
        model.transcribe = lambda _path, **kwargs: {
            'text': '原始结果包含空白片段',
            'segments': [
                {'start': 0.0, 'end': 1.0, 'text': '第一句'},
                {'start': 1.0, 'end': 2.0, 'text': '   '},
                {'start': 2.0, 'end': 3.0, 'text': '第二句'},
            ],
        }

        def fake_prepare(_audio: Path, _start: float = 0, _duration: float | None = None) -> Path:
            descriptor, name = tempfile.mkstemp(suffix='.wav')
            os.close(descriptor)
            file = Path(name)
            file.write_bytes(b'wav')
            return file

        with tempfile.NamedTemporaryFile(suffix='.webm') as source, patch.object(asr, '_validate_audio_input', return_value={'meanVolumeDb': -20.0, 'maxVolumeDb': -3.0}), patch.object(asr, '_load_model', return_value=(model, 'cpu')), patch.object(asr, '_probe_duration_seconds', return_value=3.0), patch.object(asr, '_prepare_audio_for_asr', side_effect=fake_prepare):
            result = asr.transcribe(Path(source.name), model_name='tiny')

        self.assertEqual(result['text'], '第一句 第二句')
        self.assertEqual([segment['text'] for segment in result['segments']], ['第一句', '第二句'])


if __name__ == '__main__':
    unittest.main()
