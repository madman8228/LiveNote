import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.backup import BackupError, create_backup


class BackupTests(unittest.TestCase):
    def test_backup_copies_database_data_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / 'data'
            db_path = root / 'livenote.sqlite3'
            source_audio = data_dir / 'sessions' / 'session-1' / 'segment_1' / 'chunks' / '000000.bin'
            source_audio.parent.mkdir(parents=True)
            source_audio.write_bytes(b'audio')
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript('CREATE TABLE sessions (id TEXT); CREATE TABLE chunks (id TEXT);')
                connection.execute('INSERT INTO sessions VALUES (?)', ('session-1',))
                connection.execute('INSERT INTO chunks VALUES (?)', ('chunk-1',))
                connection.commit()
            finally:
                connection.close()

            backup_dir = create_backup(data_dir, db_path, root / 'backups')
            self.assertTrue((backup_dir / 'livenote.sqlite3').is_file())
            self.assertEqual((backup_dir / 'data' / 'sessions' / 'session-1' / 'segment_1' / 'chunks' / '000000.bin').read_bytes(), b'audio')
            manifest = (backup_dir / 'manifest.json').read_text(encoding='utf-8')
            self.assertIn('"sessions": 1', manifest)
            copied = sqlite3.connect(backup_dir / 'livenote.sqlite3')
            try:
                self.assertEqual(copied.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
            finally:
                copied.close()

    def test_backup_rejects_destination_inside_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / 'data'
            data_dir.mkdir()
            db_path = root / 'livenote.sqlite3'
            sqlite3.connect(db_path).close()
            with self.assertRaises(BackupError):
                create_backup(data_dir, db_path, data_dir / 'backups')


if __name__ == '__main__':
    unittest.main()
