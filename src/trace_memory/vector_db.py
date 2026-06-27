import sqlite3
import struct
import threading
import numpy as np


class ConversationVector:

    def __init__(self, message_id, message_index, role, text, embedding, timestamp, thread_path, similarity=0.0):
        self.message_id = message_id
        self.message_index = message_index
        self.role = role
        self.text = text
        self.embedding = embedding
        self.timestamp = timestamp
        self.thread_path = thread_path
        self.similarity = similarity

def pack_float_vector(vector):
    if not vector:
        return b''
    format_string = '<' + str(len(vector)) + 'f'
    return struct.pack(format_string, *vector)

def unpack_float_vector(blob):
    if not blob:
        return []
    num_floats = len(blob) // 4
    format_string = '<' + str(num_floats) + 'f'
    unpacked = struct.unpack(format_string, blob)
    result = []
    for val in unpacked:
        result.append(val)
    return result

def cosine_similarity(v1, v2):
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    vec1 = np.array(v1, dtype=np.float32)
    vec2 = np.array(v2, dtype=np.float32)
    norm_v1 = float(np.linalg.norm(vec1))
    norm_v2 = float(np.linalg.norm(vec2))
    if norm_v1 == 0.0 or norm_v2 == 0.0:
        return 0.0
    return float(np.dot(vec1, vec2) / (norm_v1 * norm_v2))

class VectorDatabase:
    """
    B.4 FIX: Replaced the open-a-new-connection-per-call pattern with a single
    persistent SQLite connection shared across all methods.

    SQLite allows multi-threaded access with check_same_thread=False.  A
    threading.Lock() serialises writes so concurrent calls from the asyncio
    thread-pool executor never produce 'database is locked' errors.

    WAL (Write-Ahead Logging) mode is enabled so readers never block writers.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        # B.4 FIX: One persistent connection for the lifetime of this VectorDatabase.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL mode: readers and the single writer don't block each other.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Serialise writes from multiple threads (run_in_executor calls).
        self._write_lock = threading.Lock()
        self._initialize_database()

    def _initialize_database(self):
        with self._write_lock:
            cursor = self._conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_vectors (
                    message_id TEXT PRIMARY KEY,
                    message_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    timestamp REAL NOT NULL,
                    thread_path TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS topic_summaries (
                    node_id     TEXT PRIMARY KEY,
                    topic_name  TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    embedding   BLOB NOT NULL,
                    start_index INTEGER,
                    end_index   INTEGER,
                    depth       INTEGER
                )
            """)
            self._conn.commit()

    def add_conversation_message(self, msg):
        packed_vector = pack_float_vector(msg.embedding)
        with self._write_lock:
            cursor = self._conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO conversation_vectors ('
                '    message_id, message_index, role, content, embedding, timestamp, thread_path'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (msg.message_id, msg.message_index, msg.role, msg.text,
                 packed_vector, msg.timestamp, msg.thread_path)
            )
            self._conn.commit()

    def search_conversation(self, query_vector, top_k=2, min_similarity=0.5):
        if not query_vector:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            'SELECT message_id, message_index, role, content, embedding, timestamp, thread_path '
            'FROM conversation_vectors'
        )
        rows = cursor.fetchall()
        scored_msgs = []
        for row in rows:
            msg_id, msg_idx, role, content, raw_embed, timestamp, thread_path = row
            stored_vector = unpack_float_vector(raw_embed)
            sim = cosine_similarity(query_vector, stored_vector)
            if sim >= min_similarity:
                scored_msgs.append((sim, (msg_id, msg_idx, role, content, timestamp, thread_path)))
        scored_msgs.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, data in scored_msgs[:top_k]:
            msg_id, msg_idx, role, content, timestamp, thread_path = data
            msg = ConversationVector(
                message_id=msg_id, message_index=msg_idx, role=role,
                text=content, embedding=[], timestamp=timestamp,
                thread_path=thread_path, similarity=sim,
            )
            results.append(msg)
        return results

    # ── Topic Summary Methods (Surgical Retrieval) ────────────────────────────

    def upsert_topic_summary(self, node_id, topic_name, summary, embedding,
                             start_index=None, end_index=None, depth=None):
        """Insert or replace a topic embedding row. Called when a node is frozen & summarised."""
        packed = pack_float_vector(embedding)
        with self._write_lock:
            cursor = self._conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO topic_summaries '
                '(node_id, topic_name, summary, embedding, start_index, end_index, depth) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (node_id, topic_name, summary, packed, start_index, end_index, depth)
            )
            self._conn.commit()

    def search_topic_summaries(self, query_vector, top_k=3, min_similarity=0.40):
        """Cosine-search the topic_summaries table.
        Returns list of dicts: [{node_id, topic_name, summary, similarity}]
        """
        if not query_vector:
            return []
        cursor = self._conn.cursor()
        cursor.execute(
            'SELECT node_id, topic_name, summary, embedding FROM topic_summaries'
        )
        rows = cursor.fetchall()
        scored = []
        for row in rows:
            node_id, topic_name, summary, raw_embed = row
            stored_vec = unpack_float_vector(raw_embed)
            sim = cosine_similarity(query_vector, stored_vec)
            if sim >= min_similarity:
                scored.append((sim, node_id, topic_name, summary))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, node_id, topic_name, summary in scored[:top_k]:
            results.append({
                'node_id': node_id,
                'topic_name': topic_name,
                'summary': summary,
                'similarity': sim,
            })
        return results

    def delete_topic_summary(self, node_id):
        """Remove a topic summary row by node_id (used by reorganizer on merge)."""
        with self._write_lock:
            cursor = self._conn.cursor()
            cursor.execute('DELETE FROM topic_summaries WHERE node_id = ?', (node_id,))
            self._conn.commit()