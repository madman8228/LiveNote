import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.retention import delete_session_data


class RetentionTests(unittest.TestCase):
    def test_delete_session_removes_rows_and_derived_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / 'data'
            db_path = root / 'livenote.sqlite3'
            chunk_path = data_dir / 'sessions' / 'session-1' / 'segment_1' / 'chunks' / '000000.bin'
            chunk_path.parent.mkdir(parents=True)
            chunk_path.write_bytes(b'audio')
            (data_dir / 'reconstructed' / 'sessions' / 'session-1').mkdir(parents=True)
            (data_dir / 'processed' / 'sessions' / 'session-1').mkdir(parents=True)
            jobs_dir = data_dir / 'processed' / 'jobs'
            jobs_dir.mkdir(parents=True)
            (jobs_dir / 'job-1.json').write_text(json.dumps({'sessionId': 'session-1'}), encoding='utf-8')

            connection = sqlite3.connect(db_path)
            try:
                connection.executescript('''
                    CREATE TABLE sessions (id TEXT PRIMARY KEY);
                    CREATE TABLE segments (id TEXT PRIMARY KEY, session_id TEXT);
                    CREATE TABLE chunks (session_id TEXT, segment_id TEXT, local_path TEXT);
                    CREATE TABLE markers (id TEXT, session_id TEXT);
                ''')
                connection.execute('INSERT INTO sessions VALUES (?)', ('session-1',))
                connection.execute('INSERT INTO segments VALUES (?, ?)', ('segment-1', 'session-1'))
                connection.execute('INSERT INTO chunks VALUES (?, ?, ?)', ('session-1', 'segment-1', str(chunk_path.relative_to(data_dir))))
                connection.execute('INSERT INTO markers VALUES (?, ?)', ('marker-1', 'session-1'))
                connection.commit()
            finally:
                connection.close()

            counts = delete_session_data(data_dir, db_path, 'session-1')

            self.assertEqual(counts, {'segments': 1, 'chunks': 1, 'jobs': 1})
            self.assertFalse(chunk_path.exists())
            self.assertFalse((data_dir / 'sessions' / 'session-1').exists())
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0], 0)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
