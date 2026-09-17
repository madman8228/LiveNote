import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.backup import BackupError, create_backup
from server.restore import RestoreError, inspect_backup, restore_backup


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

    def test_restore_verifies_and_restores_to_empty_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_data = root / 'source-data'
            source_db = root / 'source.sqlite3'
            source_file = source_data / 'sessions' / 'session-1' / 'audio.bin'
            source_file.parent.mkdir(parents=True)
            source_file.write_bytes(b'audio')
            connection = sqlite3.connect(source_db)
            try:
                connection.execute('CREATE TABLE sessions (id TEXT)')
                connection.execute('INSERT INTO sessions VALUES (?)', ('session-1',))
                connection.commit()
            finally:
                connection.close()

            backup_dir = create_backup(source_data, source_db, root / 'backups')
            inspection = inspect_backup(backup_dir)
            self.assertEqual(inspection['counts']['sessions'], 1)

            restored_data = root / 'restored-data'
            restored_db = root / 'restored.sqlite3'
            result = restore_backup(backup_dir, restored_data, restored_db)
            self.assertEqual(result['counts']['sessions'], 1)
            self.assertEqual((restored_data / 'sessions' / 'session-1' / 'audio.bin').read_bytes(), b'audio')
            restored = sqlite3.connect(restored_db)
            try:
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
            finally:
                restored.close()

    def test_restore_refuses_existing_targets_without_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_data = root / 'source-data'
            source_data.mkdir()
            source_db = root / 'source.sqlite3'
            sqlite3.connect(source_db).close()
            backup_dir = create_backup(source_data, source_db, root / 'backups')

            target_data = root / 'target-data'
            target_data.mkdir()
            target_db = root / 'target.sqlite3'
            sqlite3.connect(target_db).close()
            with self.assertRaises(RestoreError):
                restore_backup(backup_dir, target_data, target_db)

    def test_replace_keeps_previous_targets_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_data = root / 'source-data'
            source_data.mkdir()
            (source_data / 'new.txt').write_text('new', encoding='utf-8')
            source_db = root / 'source.sqlite3'
            source_connection = sqlite3.connect(source_db)
            source_connection.execute('CREATE TABLE marker (value TEXT)')
            source_connection.execute('INSERT INTO marker VALUES (?)', ('new',))
            source_connection.commit()
            source_connection.close()
            backup_dir = create_backup(source_data, source_db, root / 'backups')

            target_data = root / 'target-data'
            target_data.mkdir()
            (target_data / 'old.txt').write_text('old', encoding='utf-8')
            target_db = root / 'target.sqlite3'
            target_connection = sqlite3.connect(target_db)
            target_connection.execute('CREATE TABLE marker (value TEXT)')
            target_connection.execute('INSERT INTO marker VALUES (?)', ('old',))
            target_connection.commit()
            target_connection.close()

            result = restore_backup(backup_dir, target_data, target_db, replace=True)
            self.assertEqual((target_data / 'new.txt').read_text(encoding='utf-8'), 'new')
            self.assertFalse((target_data / 'old.txt').exists())
            self.assertIsNotNone(result['previousDataDirectory'])
            self.assertIsNotNone(result['previousDatabase'])
            self.assertEqual(Path(result['previousDataDirectory'], 'old.txt').read_text(encoding='utf-8'), 'old')
            previous_connection = sqlite3.connect(result['previousDatabase'])
            try:
                self.assertEqual(previous_connection.execute('SELECT value FROM marker').fetchone()[0], 'old')
            finally:
                previous_connection.close()


if __name__ == '__main__':
    unittest.main()
