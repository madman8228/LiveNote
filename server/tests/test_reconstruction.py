import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import reconstruction


class ReconstructionTests(unittest.TestCase):
    def test_session_rejects_non_contiguous_segment_indexes(self) -> None:
        with self.assertRaises(reconstruction.ReconstructionError):
            reconstruction.reconstruct_session(
                Path('data'),
                'session-test',
                [(1, Path('segment-1.webm')), (3, Path('segment-3.webm'))],
            )

    def test_multi_segment_concat_uses_audio_reencode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'session.webm'
            commands: list[list[str]] = []

            def fake_run(command: list[str], timeout: int = 300) -> None:
                commands.append(command)
                Path(command[-1]).write_bytes(b'webm')

            with patch.object(reconstruction, '_run', side_effect=fake_run), patch.object(reconstruction, '_validate_media'):
                reconstruction._concat_segments([Path('segment-1.webm'), Path('segment-2.webm')], output)

            self.assertEqual(len(commands), 1)
            self.assertIn('-f', commands[0])
            self.assertIn('concat', commands[0])
            self.assertIn('-c:a', commands[0])
            self.assertIn('libopus', commands[0])
            self.assertTrue(output.is_file())

    def test_single_segment_is_remuxed_without_reencoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'session.webm'
            source = Path(directory) / 'segment-1.webm'
            source.write_bytes(b'segment')
            commands: list[list[str]] = []

            def fake_run(command: list[str], timeout: int = 300) -> None:
                commands.append(command)
                Path(command[-1]).write_bytes(b'webm')

            with patch.object(reconstruction, '_run', side_effect=fake_run), patch.object(reconstruction, '_validate_media'):
                result = reconstruction.reconstruct_session(Path(directory), 'session-test', [(1, source)])

            self.assertEqual(result.name, 'session.webm')
            self.assertTrue(result.is_file())
            self.assertEqual(commands[0][commands[0].index('-c') + 1], 'copy')


if __name__ == '__main__':
    unittest.main()
