"""Persistent symbol/source caches under ~/.perflens/cache.

Restarting the server against the same binary used to mean re-running
readelf over the whole symbol table and re-resolving every address through
addr2line — minutes of warmup on GB-scale debug binaries. These caches make
restarts near-instant:

  - symbols.db (sqlite): addr2line resolutions, inline chains, symbol
    tables, and DWARF source-file lists, keyed by binary identity
    (realpath + mtime + size — a rebuild invalidates naturally).
  - source_index_<sha1(dir)>.json.gz: the basename -> [paths] index of the
    source tree, loaded instantly at startup and refreshed in background.

Override the root with PERFLENS_HOME (used by tests).
"""

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import threading


def perflens_home():
    return (os.environ.get('PERFLENS_HOME')
            or os.path.join(os.path.expanduser('~'), '.perflens'))


def cache_dir():
    d = os.path.join(perflens_home(), 'cache')
    os.makedirs(d, exist_ok=True)
    return d


def binary_key(path):
    """Identity of a binary: realpath + mtime + size."""
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        return '%s:%d:%d' % (real, int(st.st_mtime), st.st_size)
    except OSError:
        return None


class SymbolCache:
    """sqlite-backed persistent cache. All methods are thread-safe and
    swallow sqlite errors — the cache is an accelerator, never a
    correctness dependency."""

    def __init__(self, path=None):
        self.path = path or os.path.join(cache_dir(), 'symbols.db')
        self._lock = threading.Lock()
        self._conn = None
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._conn.executescript('''
                CREATE TABLE IF NOT EXISTS addr2line (
                    bkey TEXT NOT NULL, vaddr INTEGER NOT NULL,
                    file TEXT, line INTEGER,
                    PRIMARY KEY (bkey, vaddr)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS inline_chains (
                    bkey TEXT NOT NULL, vaddr INTEGER NOT NULL,
                    chain TEXT,
                    PRIMARY KEY (bkey, vaddr)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS symtab (
                    bkey TEXT NOT NULL, name TEXT NOT NULL, addr INTEGER,
                    PRIMARY KEY (bkey, name)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS symtab_meta (
                    bkey TEXT PRIMARY KEY, count INTEGER);
                CREATE TABLE IF NOT EXISTS dwarf_files (
                    bkey TEXT PRIMARY KEY, files TEXT);
            ''')
            self._conn.commit()
        except sqlite3.Error as e:
            print('[symcache] disabled (%s)' % e, file=sys.stderr)
            self._conn = None

    # -- addr2line resolutions -------------------------------------------

    def load_addr2line(self, bkey):
        """{vaddr: (file, line)} for a binary, or {}."""
        if not self._conn or not bkey:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    'SELECT vaddr, file, line FROM addr2line WHERE bkey=?',
                    (bkey,)).fetchall()
            return {v: (f, l) for v, f, l in rows}
        except sqlite3.Error:
            return {}

    def store_addr2line(self, bkey, entries):
        """entries: {vaddr: (file, line)}"""
        if not self._conn or not bkey or not entries:
            return
        try:
            with self._lock:
                self._conn.executemany(
                    'INSERT OR REPLACE INTO addr2line VALUES (?,?,?,?)',
                    [(bkey, v, f, l) for v, (f, l) in entries.items()])
                self._conn.commit()
        except sqlite3.Error:
            pass

    # -- inline chains ----------------------------------------------------

    def load_inline(self, bkey):
        """{vaddr: [(func,file,line),...] or None} for a binary, or {}."""
        if not self._conn or not bkey:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    'SELECT vaddr, chain FROM inline_chains WHERE bkey=?',
                    (bkey,)).fetchall()
            out = {}
            for v, chain in rows:
                parsed = json.loads(chain) if chain else None
                out[v] = ([tuple(e) for e in parsed]
                          if parsed is not None else None)
            return out
        except (sqlite3.Error, ValueError):
            return {}

    def store_inline(self, bkey, entries):
        if not self._conn or not bkey or not entries:
            return
        try:
            with self._lock:
                self._conn.executemany(
                    'INSERT OR REPLACE INTO inline_chains VALUES (?,?,?)',
                    [(bkey, v, json.dumps(chain) if chain is not None else None)
                     for v, chain in entries.items()])
                self._conn.commit()
        except sqlite3.Error:
            pass

    # -- symbol tables ----------------------------------------------------

    def load_symtab(self, bkey):
        """{name: addr} for a binary, or None when not cached."""
        if not self._conn or not bkey:
            return None
        try:
            with self._lock:
                meta = self._conn.execute(
                    'SELECT count FROM symtab_meta WHERE bkey=?',
                    (bkey,)).fetchone()
                if meta is None:
                    return None
                rows = self._conn.execute(
                    'SELECT name, addr FROM symtab WHERE bkey=?',
                    (bkey,)).fetchall()
            return dict(rows)
        except sqlite3.Error:
            return None

    def store_symtab(self, bkey, symbols):
        if not self._conn or not bkey:
            return
        try:
            with self._lock:
                self._conn.execute(
                    'DELETE FROM symtab WHERE bkey=?', (bkey,))
                self._conn.executemany(
                    'INSERT OR REPLACE INTO symtab VALUES (?,?,?)',
                    [(bkey, n, a) for n, a in symbols.items()])
                self._conn.execute(
                    'INSERT OR REPLACE INTO symtab_meta VALUES (?,?)',
                    (bkey, len(symbols)))
                self._conn.commit()
        except sqlite3.Error:
            pass

    # -- DWARF source-file lists -------------------------------------------

    def load_dwarf_files(self, bkey):
        """Sorted file list, or None when not cached."""
        if not self._conn or not bkey:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    'SELECT files FROM dwarf_files WHERE bkey=?',
                    (bkey,)).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError):
            return None

    def store_dwarf_files(self, bkey, files):
        if not self._conn or not bkey:
            return
        try:
            with self._lock:
                self._conn.execute(
                    'INSERT OR REPLACE INTO dwarf_files VALUES (?,?)',
                    (bkey, json.dumps(files)))
                self._conn.commit()
        except sqlite3.Error:
            pass

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None


# ---------------------------------------------------------------------------
# Source-tree index persistence
# ---------------------------------------------------------------------------

def _source_index_path(source_dir):
    h = hashlib.sha1(os.path.realpath(source_dir).encode()).hexdigest()[:16]
    return os.path.join(cache_dir(), 'source_index_%s.json.gz' % h)


def load_source_index(source_dir):
    """Load a persisted basename->paths index for source_dir, or None."""
    path = _source_index_path(source_dir)
    try:
        with gzip.open(path, 'rt') as f:
            data = json.load(f)
        if data.get('dir') != os.path.realpath(source_dir):
            return None
        return data.get('index')
    except (OSError, ValueError):
        return None


def save_source_index(source_dir, index):
    """Atomically persist the source index (write tmp + rename)."""
    path = _source_index_path(source_dir)
    tmp = path + '.tmp.%d' % os.getpid()
    try:
        with gzip.open(tmp, 'wt', compresslevel=1) as f:
            json.dump({
                'dir': os.path.realpath(source_dir),
                'count': sum(len(v) for v in index.values()),
                'index': index,
            }, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
