import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from flask import Flask
from peewee import SqliteDatabase
from playhouse.dataset import DataSet

from sqlite_web import api
from sqlite_web import sqlite_web as sw
from sqlite_web import vue
from sqlite_web.executor import Result
from sqlite_web.executor import is_read
from sqlite_web.executor import key_decode
from sqlite_web.executor import key_encode
from sqlite_web.executor import run_one
from sqlite_web.executor import run_script
from sqlite_web.executor import split_statements
from sqlite_web.executor import wrap


class BaseExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(':memory:')
        self.dataset = DataSet(self.db)
        self.dataset.query('CREATE TABLE users (id INTEGER PRIMARY KEY, '
                           'username TEXT)')
        for username in ('huey', 'mickey', 'zaizee'):
            self.dataset.query('INSERT INTO users (username) VALUES (?)',
                               (username,))

    def user_count(self):
        return self.dataset.query('SELECT COUNT(*) FROM users').fetchone()[0]


class TestRunOne(BaseExecutorTestCase):
    def test_read_paginates(self):
        r = run_one(self.dataset, 'SELECT * FROM users', page_size=2)
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(r.columns, ['id', 'username'])
        self.assertEqual(len(r.rows), 2)
        self.assertTrue(r.has_next)
        self.assertIsNone(r.keys)

        r = run_one(self.dataset, 'SELECT * FROM users', page=2, page_size=2)
        self.assertEqual(len(r.rows), 1)
        self.assertFalse(r.has_next)

    def test_page_bounds(self):
        r = run_one(self.dataset, 'SELECT * FROM users', page=0, page_size=2)
        self.assertEqual(len(r.rows), 2)

        r = run_one(self.dataset, 'SELECT * FROM users', page=99, page_size=2)
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(r.rows, [])
        self.assertFalse(r.has_next)

    def test_ordering(self):
        r = run_one(self.dataset, 'SELECT * FROM users', ordering=-2)
        self.assertEqual(r.rows[0][1], 'zaizee')
        r = run_one(self.dataset, 'SELECT * FROM users', ordering=2)
        self.assertEqual(r.rows[0][1], 'huey')

    def test_write_autocommits(self):
        r = run_one(self.dataset, "INSERT INTO users (username) VALUES ('x')")
        self.assertEqual(r.kind, 'affected')
        self.assertEqual(r.affected, 1)
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 4)

    def test_ddl(self):
        r = run_one(self.dataset, 'CREATE TABLE t2 (id INTEGER)')
        self.assertEqual(r.kind, 'affected')
        self.assertEqual(r.affected, -1)
        self.assertEqual(run_one(self.dataset, 'SELECT * FROM t2').kind,
                         'rows')

    def test_pragma(self):
        r = run_one(self.dataset, 'PRAGMA journal_mode')
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(len(r.rows), 1)

    def test_returning(self):
        r = run_one(self.dataset,
                    "INSERT INTO users (username) VALUES ('r') RETURNING *")
        self.assertEqual(r.kind, 'rows')
        self.assertEqual(len(r.rows), 1)
        self.assertEqual(self.user_count(), 4)

    def test_error(self):
        r = run_one(self.dataset, 'SELECT nocolumn FROM users')
        self.assertEqual(r.kind, 'error')
        self.assertIn('nocolumn', r.error)

    def test_multi_statement_is_error(self):
        r = run_one(self.dataset, 'SELECT 1; SELECT 2')
        self.assertEqual(r.kind, 'error')

    def test_trailing_junk(self):
        for sql in ('SELECT * FROM users -- a comment',
                    'SELECT * FROM users;',
                    'SELECT * FROM users; \n ;'):
            r = run_one(self.dataset, sql)
            self.assertEqual(r.kind, 'rows', sql)
            self.assertEqual(len(r.rows), 3, sql)


class TestSplitStatements(unittest.TestCase):
    def test_split(self):
        script = ('CREATE TABLE t1 (id INTEGER);\n'
                  'CREATE TRIGGER trg AFTER INSERT ON t1 BEGIN '
                  'UPDATE t1 SET id = id; END;\n'
                  'INSERT INTO t1 VALUES (1);')
        stmts = split_statements(script)
        self.assertEqual(len(stmts), 3)
        self.assertTrue(stmts[1].startswith('CREATE TRIGGER'))
        self.assertTrue(stmts[1].endswith('END;'))

    def test_semicolon_in_string(self):
        self.assertEqual(split_statements("SELECT ';'; SELECT 2;"),
                         ["SELECT ';';", 'SELECT 2;'])

    def test_trailing_comment_chunk(self):
        self.assertEqual(split_statements('SELECT 1; -- done'),
                         ['SELECT 1;', '-- done'])


class TestRunScript(BaseExecutorTestCase):
    def run_sql(self, script, **kwargs):
        return run_script(self.dataset, split_statements(script), **kwargs)

    def test_statements_apply_independently(self):
        results = self.run_sql(
            "INSERT INTO users (username) VALUES ('a');"
            "INSERT INTO users (username) VALUES ('b');")
        self.assertEqual([r.kind for r in results], ['affected', 'affected'])
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 5)

    def test_stops_at_first_error(self):
        results = self.run_sql(
            "INSERT INTO users (username) VALUES ('a');"
            'CREATE TABLE t2 (id INTEGER);'
            'SELECT nocolumn FROM users;'
            "INSERT INTO users (username) VALUES ('never');")
        self.assertEqual([r.kind for r in results],
                         ['affected', 'affected', 'error'])
        # Statements before the error stay applied, DDL included.
        self.assertEqual(self.user_count(), 4)
        self.assertEqual(run_one(self.dataset, 'SELECT * FROM t2').kind,
                         'rows')

    def test_select_inside_script(self):
        results = self.run_sql(
            'SELECT * FROM users; SELECT COUNT(*) FROM users;', page_size=2)
        self.assertEqual(len(results[0].rows), 2)
        self.assertTrue(results[0].has_next)
        self.assertEqual(results[1].rows[0][0], 3)

    def test_user_owned_transaction(self):
        results = self.run_sql(
            "BEGIN; INSERT INTO users (username) VALUES ('a'); COMMIT;")
        self.assertEqual([r.kind for r in results], ['affected'] * 3)
        self.assertFalse(self.db.connection().in_transaction)
        self.assertEqual(self.user_count(), 4)

        self.run_sql(
            "BEGIN; INSERT INTO users (username) VALUES ('b'); ROLLBACK;")
        self.assertEqual(self.user_count(), 4)

    def test_dangling_begin_rolls_back_on_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SqliteDatabase(os.path.join(tmpdir, 'x.db'))
            ds = DataSet(db)
            ds.query('CREATE TABLE t1 (id INTEGER)')
            results = run_script(ds, split_statements(
                'BEGIN; INSERT INTO t1 VALUES (1); SELECT nocolumn FROM t1;'))
            self.assertEqual(results[-1].kind, 'error')
            self.assertTrue(db.connection().in_transaction)
            # Request teardown closes the connection; sqlite rolls back.
            db.close()
            db.connect()
            self.assertEqual(
                ds.query('SELECT COUNT(*) FROM t1').fetchone()[0], 0)
            db.close()


class TestIsRead(BaseExecutorTestCase):
    def test_is_read(self):
        self.assertTrue(is_read(self.dataset, 'SELECT * FROM users'))
        self.assertTrue(is_read(self.dataset, 'SELECT * FROM users -- x'))
        self.assertFalse(is_read(self.dataset, 'DROP TABLE users'))
        self.assertFalse(is_read(self.dataset,
                                 "UPDATE users SET username = 'x'"))
        self.assertFalse(is_read(self.dataset, 'SELECT 1; SELECT 2'))
        self.assertEqual(self.user_count(), 3)


class TestWrap(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(wrap('SELECT 1;'),
                         'SELECT * FROM (\nSELECT 1\n) AS _')
        self.assertEqual(wrap('SELECT 1', ordering=-2),
                         'SELECT * FROM (\nSELECT 1\n) AS _ ORDER BY 2 DESC')
        self.assertEqual(wrap('SELECT 1', ordering=2, limit=51, offset=50),
                         'SELECT * FROM (\nSELECT 1\n) AS _ '
                         'ORDER BY 2 ASC LIMIT 51 OFFSET 50')
        self.assertEqual(wrap('SELECT 1', limit=0),
                         'SELECT * FROM (\nSELECT 1\n) AS _ LIMIT 0 OFFSET 0')
        self.assertEqual(wrap('SELECT 1', select='COUNT(*)'),
                         'SELECT COUNT(*) FROM (\nSELECT 1\n) AS _')


class TestRowKey(unittest.TestCase):
    def test_round_trips(self):
        for values in ([42], ['abc'], [b'\x01\x02\xff'], ['US', 'A:::B'],
                       [None, 1.5], ['✓']):
            self.assertEqual(key_decode(key_encode(values)), values)

    def test_url_safe(self):
        token = key_encode([b'\xfb\xff' * 30])
        self.assertNotIn('+', token)
        self.assertNotIn('/', token)


class BaseAppTestCase(unittest.TestCase):
    SCHEMA = """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        INSERT INTO users (username) VALUES ('huey'), ('mickey'), ('zaizee');
        CREATE TABLE comp (a TEXT, b TEXT, label TEXT, PRIMARY KEY (a, b));
        INSERT INTO comp VALUES ('US', 'A:::B', 'composite-row');
        CREATE TABLE blobs (id BLOB PRIMARY KEY, note TEXT);
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO parent (name) VALUES ('p-one');
        CREATE TABLE child (id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parent, label TEXT);
        INSERT INTO child (parent_id, label) VALUES (1, 'c-one');
        CREATE TABLE nopk (a TEXT);
        INSERT INTO nopk VALUES ('no-pk-row');
        CREATE TABLE oddpk ("user id" INTEGER NOT NULL, grp TEXT NOT NULL,
            val TEXT, PRIMARY KEY ("user id", grp));
        INSERT INTO oddpk VALUES (7, 'a', 'odd-row'), (8, 'a', 'same-grp');
        CREATE TABLE tag (name TEXT PRIMARY KEY);
        INSERT INTO tag VALUES (''), ('red');
        CREATE TABLE post (id INTEGER PRIMARY KEY,
            tag TEXT REFERENCES tag(name), body TEXT);
        CREATE VIEW v_users AS SELECT * FROM users;
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, 'app.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        conn.execute('INSERT INTO blobs VALUES (?, ?)', (b'\x00\xff', 'blob'))
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path])
        sw.app.config['TESTING'] = True
        sw.app.config['USE_OLD_UI'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dbrows(self, sql, *params):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows


class TestExecutionPolicy(BaseAppTestCase):
    def test_cross_site_post_rejected(self):
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/query/', data={'sql': 'SELECT 1'})
        self.assertEqual(r.status_code, 200)

    def test_get_does_not_execute_writes(self):
        self.client.get('/query/', query_string={'sql': 'DELETE FROM users'})
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

    def test_post_runs_write_and_ddl(self):
        r = self.client.post('/query/',
                             data={'sql': "INSERT INTO users (username) "
                                          "VALUES ('x')"})
        self.assertIn(b'Rows modified', r.data)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 4)
        self.client.post('/query/', data={'sql': 'CREATE TABLE t2 (id INT)'})
        self.assertEqual(self.client.get('/t2/').status_code, 200)

    def test_export_refuses_non_select(self):
        r = self.client.post('/query/', data={'sql': 'DROP TABLE users',
                                              'export_csv': '1'})
        self.assertIn(b'Only a single query may be exported', r.data)
        self.assertTrue(self.dbrows('SELECT COUNT(*) FROM users'))


class TestValueFilter(unittest.TestCase):
    def setUp(self):
        self._truncate = sw.app.config['TRUNCATE_VALUES']

    def tearDown(self):
        sw.app.config['TRUNCATE_VALUES'] = self._truncate

    def test_link_requires_full_match(self):
        url = 'https://example.com/x'
        self.assertEqual(sw.value_filter(url),
                         '<a href="%s">%s</a>' % (url, url))
        self.assertNotIn('<a ', sw.value_filter(url + ' trailing text'))

    def test_mailto(self):
        self.assertIn('<a href="mailto:huey@example.com"',
                      sw.value_filter('mailto:huey@example.com'))

    def test_long_link_label_truncated(self):
        url = 'https://example.com/' + 'x' * 60
        out = sw.value_filter(url)
        self.assertIn('href="%s"' % url, out)
        self.assertIn('...', out)

    def test_multiline_value_wrapped(self):
        self.assertEqual(sw.value_filter('line one\nline two'),
                         '<span class="pre">line one\nline two</span>')
        self.assertEqual(sw.value_filter('plain'), 'plain')

    def test_blob_respects_truncate_flag(self):
        data = b'\xff' * 600  # Undecodable, 1200 hex chars.
        sw.app.config['TRUNCATE_VALUES'] = True
        self.assertNotIn('ff' * 600, sw.value_filter(data))
        sw.app.config['TRUNCATE_VALUES'] = False
        self.assertIn('ff' * 600, sw.value_filter(data))


class TestExplain(BaseAppTestCase):
    def test_explain_select(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM users',
                                              'explain': '1'})
        self.assertIn(b'SCAN', r.data)

    def test_explain_compiles_writes_without_running(self):
        self.client.post('/query/', data={
            'sql': "INSERT INTO users (username) VALUES ('x')",
            'explain': '1'})
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

    def test_explain_suppresses_row_keys(self):
        # Plan rows have an "id" column, which must not become edit links.
        r = self.client.post('/users/query/', data={
            'sql': 'SELECT * FROM users', 'explain': '1'})
        self.assertNotIn(b'/users/update/', r.data)
        self.assertNotIn(b'/users/row/', r.data)
        self.assertNotIn(b'name="count"', r.data)


class TestPaginationGating(BaseAppTestCase):
    def test_read_paginates(self):
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'name="count"', r.data)
        self.assertIn(b'bulk-action', r.data)

    def test_returning_hides_pagination_and_bulk(self):
        # The count button and bulk form re-submit the sql. For a write
        # that would execute it again.
        r = self.client.post('/users/query/', data={
            'sql': "INSERT INTO users (username) VALUES ('r') RETURNING *"})
        self.assertNotIn(b'name="count"', r.data)
        self.assertNotIn(b'bulk-action', r.data)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 4)

    def test_query_tab_bulk_delete(self):
        r = self.client.post('/users/query/', data={
            'sql': 'SELECT * FROM users', 'action': 'bulk-delete',
            'pk': key_encode([1])})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 2)
        self.assertIn(b'bulk-action', r.data)  # Fresh results offer bulk.


class TestForeignKeyLinks(BaseAppTestCase):
    def test_content_links_fk_values(self):
        r = self.client.get('/child/content/')
        self.assertIn(b'/parent/query/', r.data)

    def test_table_query_links_fk_values(self):
        r = self.client.post('/child/query/',
                             data={'sql': 'SELECT * FROM child'})
        self.assertIn(b'/parent/query/', r.data)

    def test_structure_shows_fk_target(self):
        r = self.client.get('/child/')
        self.assertIn(b'<code>parent.id</code>', r.data)

    def test_fk_link_resolves(self):
        r = self.client.get('/parent/query/', query_string={
            'sql': 'SELECT * FROM "parent" WHERE "id" = 1'})
        self.assertIn(b'p-one', r.data)

    def test_no_fk_links_on_generic_query(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM child'})
        self.assertNotIn(b'/parent/query/', r.data)


class TestLastViewed(BaseAppTestCase):
    def test_single_capped_session_key(self):
        with self.client.session_transaction() as s:
            s['users.last_viewed'] = [5, None]  # Legacy per-table key.
        self.client.get('/users/content/')
        self.client.get('/child/content/')
        with self.client.session_transaction() as s:
            self.assertNotIn('users.last_viewed', s)
            self.assertEqual([e[0] for e in s['last_viewed']],
                             ['child', 'users'])

    def test_back_links_carry_saved_position(self):
        # Real destination urls rendered into the hrefs, no bounce route.
        with self.client.session_transaction() as s:
            s['last_viewed'] = [['users', 3, -2]]
        for url in ('/users/row/%s/' % key_encode([1]),
                    '/users/update/%s/' % key_encode([1])):
            r = self.client.get(url)
            self.assertIn(b'page=3', r.data, url)
            self.assertIn(b'ordering=-2', r.data, url)
        # No saved entry falls back to plain content.
        r = self.client.get('/child/row/%s/' % key_encode([1]))
        self.assertIn(b'href="/child/content/"', r.data)

    def test_redirect_to_previous_uses_saved_position(self):
        with self.client.session_transaction() as s:
            s['last_viewed'] = [['users', 3, -2]]
        r = self.client.post('/users/delete/%s/' % key_encode([1]))
        self.assertIn(r.status_code, (302, 303))
        self.assertIn('page=3', r.headers['Location'])
        self.assertIn('ordering=-2', r.headers['Location'])


class TestRowKeyRoutes(BaseAppTestCase):
    def test_composite_key_with_delimiter(self):
        token = key_encode(['US', 'A:::B'])
        r = self.client.get('/comp/update/%s/' % token)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'composite-row', r.data)

    def test_blob_update_form_renders_hex(self):
        r = self.client.get('/blobs/update/%s/' % key_encode([b'\x00\xff']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'00ff', r.data)

    def test_blob_bulk_delete(self):
        token = key_encode([b'\x00\xff'])
        r = self.client.post('/blobs/content/',
                             data={'action': 'bulk-delete', 'pk': token},
                             headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM blobs')[0][0], 0)

    def test_text_pk_valued_like_old_sentinel(self):
        self.client.post('/query/', data={'sql': "INSERT INTO users (id, "
                                          "username) VALUES (42, '__uneditable__')"})
        # A row whose value collides with the retired sentinel is still edited
        # by its own pk, not the username.
        token = key_encode([42])
        r = self.client.get('/users/update/%s/' % token)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'__uneditable__', r.data)

    def test_malformed_key_404s(self):
        self.assertEqual(self.client.get('/users/update/@@bad@@/').status_code,
                         404)

    def test_short_composite_token_cannot_multi_delete(self):
        # A one-value token against the two-column pk must be refused, a
        # zip would otherwise under-constrain the WHERE.
        token = key_encode(['US'])
        r = self.client.post('/comp/delete/%s/' % token)
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM comp')[0][0], 1)
        r = self.client.get('/comp/row/%s/' % token)
        self.assertIn(r.status_code, (302, 303))

    def test_sanitized_pk_column_stays_in_key(self):
        # "user id" reflects as user_id and still keys the row. A grp-only
        # pk would target every row sharing grp.
        token = key_encode([7, 'a'])
        r = self.client.get('/oddpk/content/')
        self.assertIn(('/oddpk/row/%s/' % token).encode(), r.data)
        r = self.client.post('/oddpk/delete/%s/' % token)
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.dbrows('SELECT val FROM oddpk'), [('same-grp',)])


class TestDownload(BaseAppTestCase):
    def test_download_is_a_valid_snapshot(self):
        r = self.client.get('/download/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers['Content-Disposition'])
        self.assertIn('app.db', r.headers['Content-Disposition'])
        self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))

        path = os.path.join(self.tmp, 'snapshot.db')
        with open(path, 'wb') as f:
            f.write(r.data)
        r.close()  # Fires call_on_close, which removes the temp snapshot.
        conn = sqlite3.connect(path)
        count, = conn.execute('SELECT COUNT(*) FROM users').fetchone()
        conn.close()
        self.assertEqual(count, 3)

    def test_temp_dir_removed_after_streaming(self):
        marker = os.path.join(self.tmp, 'dl-tmp')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/')
            self.assertTrue(os.path.exists(marker))  # Held while streaming.
            self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))
            self.assertFalse(os.path.exists(marker))  # Gone once consumed.
            r.close()
        finally:
            sw.tempfile.mkdtemp = orig

    def test_head_request_does_not_leak(self):
        # HEAD never starts the body generator, so cleanup must not depend
        # on the generator running.
        marker = os.path.join(self.tmp, 'dl-head')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.head('/download/')
            self.assertEqual(r.status_code, 200)
            self.assertIn('attachment', r.headers['Content-Disposition'])
            r.close()
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig

    def test_temp_dir_removed_on_abandoned_download(self):
        # A client that disconnects mid-stream must not leak the snapshot.
        marker = os.path.join(self.tmp, 'dl-abandon')
        os.mkdir(marker)
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/')
            self.assertTrue(os.path.exists(marker))
            r.close()  # Closed without ever reading the body.
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig

    def test_error_flashes_and_cleans_up(self):
        marker = os.path.join(self.tmp, 'dl-err')
        os.mkdir(marker)
        os.chmod(marker, 0o500)  # VACUUM INTO cannot create its file.
        orig = sw.tempfile.mkdtemp
        sw.tempfile.mkdtemp = lambda: marker
        try:
            r = self.client.get('/download/', follow_redirects=True)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'Error creating database snapshot', r.data)
            self.assertFalse(os.path.exists(marker))
        finally:
            sw.tempfile.mkdtemp = orig
            if os.path.exists(marker):
                os.chmod(marker, 0o700)


class TestMultiDb(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        self.db2 = os.path.join(self.tmp, 'two.db')
        conn = sqlite3.connect(self.db2)
        conn.execute('CREATE TABLE t2 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path, self.db2])
        self.client = sw.app.test_client()

    def test_download_follows_selected_dataset(self):
        self.client.get('/select-dataset/',
                        query_string={'name': os.path.realpath(self.db2)})
        r = self.client.get('/download/')
        self.assertIn('two.db', r.headers['Content-Disposition'])
        path = os.path.join(self.tmp, 'snap2.db')
        with open(path, 'wb') as f:
            f.write(r.data)
        r.close()
        conn = sqlite3.connect(path)
        tables = [t for t, in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(tables, ['t2'])

        self.client.get('/select-dataset/',
                        query_string={'name': os.path.realpath(self.db_path)})
        r = self.client.get('/download/')
        self.assertIn('app.db', r.headers['Content-Disposition'])
        r.close()


class TestDuplicateBasenames(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        self.db2 = os.path.join(self.tmp, 'alt', 'app.db')
        os.makedirs(os.path.dirname(self.db2))
        conn = sqlite3.connect(self.db2)
        conn.execute('CREATE TABLE t2 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        sw.datasets.clear()
        sw.initialize_app([self.db_path, self.db2])
        sw.app.config['ENABLE_FILESYSTEM'] = True
        self.client = sw.app.test_client()

    def tearDown(self):
        super().tearDown()
        sw.app.config['ENABLE_FILESYSTEM'] = False

    def select(self, path):
        return self.client.get('/select-dataset/',
                               query_string={'name': os.path.realpath(path)})

    def test_both_databases_are_loaded(self):
        self.assertEqual(sorted(sw.datasets), sorted([
            os.path.realpath(self.db_path), os.path.realpath(self.db2)]))

    def test_each_database_is_selectable(self):
        self.select(self.db2)
        self.assertIn(b'href="/t2/"', self.client.get('/').data)

        self.select(self.db_path)
        self.assertIn(b'href="/users/"', self.client.get('/').data)

    def test_menu_shows_the_parent_directory(self):
        # Long paths stay in the link target, out of the menu text.
        r = self.client.get('/')
        self.assertIn(b'>alt/app.db<', r.data)
        self.assertIn(b'>%s/app.db<'
                      % os.path.basename(os.path.realpath(self.tmp)).encode(),
                      r.data)
        self.assertNotIn(b'>%s<' % os.path.realpath(self.db2).encode(), r.data)

    def test_download_uses_basename(self):
        self.select(self.db2)
        r = self.client.get('/download/')
        self.assertIn('app.db', r.headers['Content-Disposition'])
        r.close()

    def test_runtime_load_leaves_loaded_databases_alone(self):
        before = list(sw.datasets)
        third = os.path.join(self.tmp, 'other', 'app.db')
        os.makedirs(os.path.dirname(third))
        conn = sqlite3.connect(third)
        conn.execute('CREATE TABLE t3 (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        r = self.client.post('/load/', data={'mode': 'filesystem',
                                             'filename': third})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(list(sw.datasets),
                         before + [os.path.realpath(third)])
        # The newly-loaded database becomes the selected one.
        self.assertIn(b'href="/t3/"', self.client.get('/').data)

    def test_unload_removes_only_the_named_database(self):
        r = self.client.post('/unload/',
                             data={'dataset': os.path.realpath(self.db2)})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(list(sw.datasets), [os.path.realpath(self.db_path)])


class TestRowDetailEdges(BaseAppTestCase):
    def test_blob_pk_detail(self):
        r = self.client.get('/blobs/row/%s/' % key_encode([b'\x00\xff']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'blob', r.data)

    def test_extra_key_values_ignored(self):
        # Same behavior as update/delete, the first value drives the lookup.
        r = self.client.get('/users/row/%s/' % key_encode([1, 2]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)

    def test_empty_key_404s(self):
        # A token holding no values, [] or a crafted {}, must 404 instead
        # of raising IndexError in decode_pk.
        for token in (key_encode([]), 'e30='):
            for route in ('row', 'update', 'delete'):
                url = '/users/%s/%s/' % (route, token)
                self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_no_pk_table(self):
        r = self.client.get('/nopk/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'no-pk-row', r.data)
        self.assertNotIn(b'/nopk/row/', r.data)
        r = self.client.get('/nopk/row/%s/' % key_encode(['no-pk-row']))
        self.assertIn(r.status_code, (302, 303))

    def test_sql_view(self):
        r = self.client.get('/v_users/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)
        self.assertNotIn(b'/v_users/row/', r.data)
        for route in ('row', 'update', 'delete'):
            r = self.client.get('/v_users/%s/%s/' % (route, key_encode([1])))
            self.assertIn(r.status_code, (302, 303), route)


class TestRowDetail(BaseAppTestCase):
    def test_detail_page(self):
        r = self.client.get('/users/row/%s/' % key_encode([1]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'huey', r.data)
        self.assertIn(b'/users/update/', r.data)

    def test_missing_row_redirects(self):
        r = self.client.get('/users/row/%s/' % key_encode([999]))
        self.assertIn(r.status_code, (302, 303))

    def test_malformed_key_404s(self):
        self.assertEqual(self.client.get('/users/row/@@bad@@/').status_code,
                         404)

    def test_composite_pk_detail(self):
        r = self.client.get('/comp/row/%s/' % key_encode(['US', 'A:::B']))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'composite-row', r.data)

    def test_detail_links_fk_values(self):
        r = self.client.get('/child/row/%s/' % key_encode([1]))
        self.assertIn(b'/parent/query/', r.data)

    def test_view_links_on_content_and_query_tabs(self):
        self.assertIn(b'/users/row/', self.client.get('/users/content/').data)
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'/users/row/', r.data)

    def test_no_view_links_on_generic_query(self):
        r = self.client.post('/query/', data={'sql': 'SELECT * FROM users'})
        self.assertNotIn(b'/users/row/', r.data)


class TestReadOnlyRowDetail(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        sw.datasets.clear()
        sw.initialize_app([self.db_path], read_only=True)
        self.client = sw.app.test_client()

    def tearDown(self):
        super().tearDown()
        sw.dataset_config['read_only'] = False

    def test_read_only_gets_view_but_not_edit(self):
        r = self.client.get('/users/content/')
        self.assertIn(b'/users/row/', r.data)
        self.assertNotIn(b'/users/update/', r.data)
        self.assertNotIn(b'toggle-pk-all', r.data)

    def test_read_only_detail_page(self):
        r = self.client.get('/users/row/%s/' % key_encode([2]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'mickey', r.data)
        self.assertNotIn(b'/users/update/', r.data)

    def test_read_only_download(self):
        # VACUUM INTO runs against the mode=ro connection.
        r = self.client.get('/download/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b'SQLite format 3\x00'))
        r.close()

    def test_read_only_smoke(self):
        for url in ('/', '/users/', '/users/content/', '/users/query/'):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get('/users/content/')
        self.assertNotIn(b'/users/insert/', r.data)
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT * FROM users'})
        self.assertIn(b'huey', r.data)

    def test_read_only_writes_fail_at_the_database(self):
        # Enforcement is the mode=ro connection. Writes must fail safely
        # with data unchanged and no 500s.
        r = self.client.post('/users/insert/', data={
            'chk_username': 'on', 'username': 'nope'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

        r = self.client.post('/users/drop/', follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.dbrows('SELECT COUNT(*) FROM users'))

        r = self.client.post('/users/update/%s/' % key_encode([1]),
                             data={'chk_username': 'on', 'username': 'x'},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        rows = self.dbrows('SELECT username FROM users WHERE id = 1')
        self.assertEqual(rows, [('huey',)])

        r = self.client.post('/create-table/', data={
            'table_name': 'zz', 'redirect': '/'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Error', r.data)


class TestPasswordlessLogin(BaseAppTestCase):
    def test_login_redirects_when_no_password(self):
        r = self.client.get('/login/')
        self.assertIn(r.status_code, (302, 303))
        r = self.client.post('/login/', data={})
        self.assertIn(r.status_code, (302, 303))
        with self.client.session_transaction() as s:
            self.assertNotIn('authorized', s)


class TestCreateTable(BaseAppTestCase):
    def test_create_failure_keeps_flash_destination(self):
        # The sqlite_ prefix is reserved, so creation fails. The redirect
        # must go back to the caller, not to a 404ing import page.
        r = self.client.post('/create-table/', data={
            'table_name': 'sqlite_nope', 'redirect': '/'},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Error', r.data)

    def test_create_success_lands_on_import(self):
        r = self.client.post('/create-table/', data={
            'table_name': 'fresh', 'redirect': '/'})
        self.assertIn('/fresh/import/', r.headers['Location'])


class TestErrorPages(BaseAppTestCase):
    def test_404_renders_in_chrome(self):
        r = self.client.get('/nope-not-a-table/')
        self.assertEqual(r.status_code, 404)
        self.assertIn(b'Not Found', r.data)
        self.assertIn(b'powered by', r.data)

    def test_403_renders_in_chrome(self):
        r = self.client.post('/query/', data={'sql': 'SELECT 1'},
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'powered by', r.data)

    def test_empty_registry_500_renders(self):
        # The error page renders even when no dataset can be resolved.
        sw.datasets.clear()
        r = self.client.get('/')
        self.assertEqual(r.status_code, 500)
        self.assertIn(b'powered by', r.data)


class TestQueryTemplates(BaseAppTestCase):
    def test_shared_form_renders_on_both_pages(self):
        for url, textarea_id in (('/query/', b'id="sql"'),
                                 ('/users/query/', b'id="table-sql"')):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertIn(b'name="explain"', r.data)
            self.assertIn(b'id="bookmark-modal"', r.data)
            self.assertIn(b'id="import-bookmarks"', r.data)
            self.assertIn(b'__TABLE__', r.data)
            self.assertIn(b'id="sql-image-modal"', r.data)
            self.assertIn(textarea_id, r.data)

    def test_copy_affordances_present(self):
        r = self.client.get('/users/content/')
        self.assertIn(b'copy-row', r.data)
        self.assertIn(b'data-col="username"', r.data)

    def test_script_results_offer_copy(self):
        r = self.client.post('/users/query/',
                             data={'sql': 'SELECT 1; SELECT 2;'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'copy-row', r.data)
        self.assertNotIn(b'/users/row/', r.data)

    def test_missing_table_query_keeps_sql(self):
        # A bookmark can reference a dropped or foreign table. Its sql
        # falls back to the generic query page.
        r = self.client.get('/nope/query/', query_string={'sql': 'SELECT 1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/query/?sql=SELECT', r.headers['Location'])
        r = self.client.get('/nope/query/', query_string={'sql': 'SELECT 1'},
                            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'SELECT 1', r.data)

    def test_missing_table_query_404s_without_sql(self):
        self.assertEqual(self.client.get('/nope/query/').status_code, 404)


class TestInsertForm(BaseAppTestCase):
    def test_blank_numeric_inserts_null(self):
        # The form pre-enables every column, so a blank numeric input must
        # mean NULL instead of failing validation.
        r = self.client.post('/child/insert/', data={
            'chk_parent_id': 'on', 'parent_id': '',
            'chk_label': 'on', 'label': 'blank-num'})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows(
            'SELECT parent_id, label FROM child WHERE label = ?', 'blank-num')
        self.assertEqual(rows, [(None, 'blank-num')])

    def test_blank_text_inserts_empty_string(self):
        r = self.client.post('/child/insert/', data={
            'chk_label': 'on', 'label': ''})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows("SELECT COUNT(*) FROM child WHERE label = ''")
        self.assertEqual(rows, [(1,)])

    def test_blank_text_fk_keeps_empty_string(self):
        # A TEXT primary key can legitimately be '', so a blank input on
        # a text-keyed fk must not become NULL.
        r = self.client.post('/post/insert/', data={
            'chk_tag': 'on', 'tag': '',
            'chk_body': 'on', 'body': 'text-fk'})
        self.assertIn(r.status_code, (302, 303))
        rows = self.dbrows("SELECT tag FROM post WHERE body = 'text-fk'")
        self.assertEqual(rows, [('',)])


class TestContentTab(BaseAppTestCase):
    def test_renders_with_row_actions(self):
        r = self.client.get('/users/content/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'/users/update/', r.data)
        self.assertIn(b'/users/delete/', r.data)
        self.assertIn(b'toggle-pk-all', r.data)

    def test_ordinal_ordering(self):
        asc = self.client.get('/users/content/',
                              query_string={'ordering': '2'}).data
        desc = self.client.get('/users/content/',
                               query_string={'ordering': '-2'}).data
        self.assertLess(asc.index(b'huey'), asc.index(b'zaizee'))
        self.assertLess(desc.index(b'zaizee'), desc.index(b'huey'))

    def test_bad_ordering_ignored(self):
        for value in ('99', 'abc', '-abc'):
            r = self.client.get('/users/content/',
                                query_string={'ordering': value})
            self.assertEqual(r.status_code, 200)

    def test_disabled_pager_arrows_are_not_links(self):
        # Single page of data, so all four arrows must render as spans.
        r = self.client.get('/users/content/')
        for arrow in (b'&laquo;', b'&lsaquo;', b'&rsaquo;', b'&raquo;'):
            self.assertIn(b'<span class="page-link">' + arrow, r.data)

    def test_flash_alerts_are_dismissible(self):
        r = self.client.get('/users/row/%s/' % key_encode([999]),
                            follow_redirects=True)
        self.assertIn(b'alert-dismissible', r.data)
        self.assertNotIn(b'alert-dismissable', r.data)


class TestUrlPrefix(BaseAppTestCase):
    def setUp(self):
        super(TestUrlPrefix, self).setUp()
        self.wsgi_app = sw.app.wsgi_app
        sw.initialize_app([], url_prefix='/sqlite/')

    def tearDown(self):
        sw.app.wsgi_app = self.wsgi_app
        sw.app.config['SESSION_COOKIE_PATH'] = None
        super(TestUrlPrefix, self).tearDown()

    def test_session_cookie_scoped_to_prefix(self):
        r = self.client.get('/sqlite/users/content/')
        self.assertIn('Path=/sqlite', r.headers['Set-Cookie'])


class ApiTestCase(BaseAppTestCase):
    def get_json(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        return response.status_code, response.get_json()

    def post_json(self, path, payload, **kwargs):
        response = self.client.post(path, json=payload, **kwargs)
        return response.status_code, response.get_json()

    def patch_json(self, path, payload, **kwargs):
        response = self.client.patch(path, json=payload, **kwargs)
        return response.status_code, response.get_json()

    def delete_json(self, path, **kwargs):
        response = self.client.delete(path, **kwargs)
        return response.status_code, response.get_json()


class TestIndex(ApiTestCase):
    def test_index_describes_the_api(self):
        status, data = self.get_json('/api/')
        self.assertEqual(status, 200)
        self.assertIn('app.db', [os.path.basename(p) for p in data['datasets']])
        paths = {e['path'] for e in data['endpoints']}
        self.assertIn('/api/query/', paths)
        self.assertIn('/api/<table>/rows/', paths)

    def test_datasets_lists_the_loaded_database(self):
        status, data = self.get_json('/api/datasets/')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['datasets']), 1)
        self.assertEqual(data['datasets'][0]['basename'], 'app.db')
        self.assertFalse(data['datasets'][0]['is_readonly'])

    def test_unknown_route_is_a_404(self):
        status, _ = self.get_json('/api/nope/')
        self.assertEqual(status, 404)


class TestTables(ApiTestCase):
    def test_lists_tables_and_views(self):
        status, data = self.get_json('/api/tables/')
        self.assertEqual(status, 200)
        by_name = {t['name']: t for t in data['tables']}
        self.assertIn('users', by_name)
        self.assertIn('v_users', by_name)
        self.assertTrue(by_name['v_users']['is_view'])
        self.assertFalse(by_name['users']['is_view'])

    def test_reports_which_tables_can_be_addressed_by_key(self):
        _, data = self.get_json('/api/tables/')
        by_name = {t['name']: t for t in data['tables']}
        self.assertTrue(by_name['users']['has_usable_pk'])
        self.assertFalse(by_name['nopk']['has_usable_pk'])
        self.assertFalse(by_name['v_users']['has_usable_pk'])


class TestSchema(ApiTestCase):
    def test_columns_and_single_column_key(self):
        status, data = self.get_json('/api/users/schema/')
        self.assertEqual(status, 200)
        self.assertEqual([c['column'] for c in data['columns']], ['id', 'username'])
        self.assertEqual(data['primary_key'], {'composite': False, 'field': 'id', 'column': 'id'})
        self.assertTrue(data['columns'][0]['primary_key'])
        self.assertIn('CREATE TABLE', data['sql'])
        self.assertTrue(data['has_usable_pk'])
        self.assertFalse(data['is_view'])

    def test_composite_key(self):
        _, data = self.get_json('/api/oddpk/schema/')
        self.assertEqual(data['primary_key'], {'composite': True, 'fields': ['user_id', 'grp']})

    def test_column_name_is_reported_alongside_the_field_name(self):
        # Reflection sanitizes "user id" into user_id, so the row endpoints and
        # the SQL endpoint disagree unless both names are surfaced.
        _, data = self.get_json('/api/oddpk/schema/')
        first = data['columns'][0]
        self.assertEqual(first['column'], 'user id')
        self.assertEqual(first['field'], 'user_id')

    def test_table_without_a_key_reports_no_primary_key(self):
        _, data = self.get_json('/api/nopk/schema/')
        self.assertIsNone(data['primary_key'])
        self.assertFalse(data['has_usable_pk'])

    def test_foreign_keys_are_reported(self):
        _, data = self.get_json('/api/child/schema/')
        self.assertEqual(len(data['foreign_keys']), 1)
        self.assertEqual(data['foreign_keys'][0]['dest_table'], 'parent')

    def test_unknown_table_is_a_404(self):
        status, data = self.get_json('/api/nope/schema/')
        self.assertEqual(status, 404)
        self.assertIn('users', data['available'])


class TestRows(ApiTestCase):
    def test_rows_are_returned_with_keys(self):
        status, data = self.get_json('/api/users/rows/')
        self.assertEqual(status, 200)
        self.assertEqual([r['username'] for r in data['rows']], ['huey', 'mickey', 'zaizee'])
        self.assertEqual([r['id'] for r in data['rows']], [1, 2, 3])
        for row in data['rows']:
            self.assertIn('_key', row)

    def test_columns_are_listed(self):
        _, data = self.get_json('/api/users/rows/')
        self.assertEqual(data['columns'], ['id', 'username'])

    def test_pagination_reports_the_next_offset(self):
        status, data = self.get_json('/api/users/rows/?limit=2')
        self.assertTrue(data['has_next'])
        self.assertEqual(data['next_offset'], 2)
        self.assertEqual(len(data['rows']), 2)

        status, data = self.get_json('/api/users/rows/?limit=2&offset=2')
        self.assertFalse(data['has_next'])
        self.assertIsNone(data['next_offset'])
        self.assertEqual([r['username'] for r in data['rows']], ['zaizee'])

    def test_limit_is_capped(self):
        _, data = self.get_json('/api/users/rows/?limit=999999999')
        self.assertEqual(data['limit'], api.MAX_LIMIT)

    def test_filtering_by_column(self):
        _, data = self.get_json('/api/users/rows/?username=mickey')
        self.assertEqual([r['id'] for r in data['rows']], [2])

    def test_ordering(self):
        _, data = self.get_json('/api/users/rows/?order=-id')
        self.assertEqual([r['id'] for r in data['rows']], [3, 2, 1])
        _, data = self.get_json('/api/users/rows/?order=id')
        self.assertEqual([r['id'] for r in data['rows']], [1, 2, 3])

    def test_unknown_filter_column_is_rejected(self):
        status, data = self.get_json('/api/users/rows/?nope=1')
        self.assertEqual(status, 400)
        self.assertIn('id', data['columns'])

    def test_a_filter_value_is_a_value_not_sql(self):
        status, data = self.get_json("/api/users/rows/?username=' OR 1=1 --")
        self.assertEqual(status, 200)
        self.assertEqual(data['rows'], [])
        self.assertEqual(len(self.dbrows('SELECT * FROM users')), 3)

    def test_order_is_a_column_not_sql(self):
        status, _ = self.get_json('/api/users/rows/?order=id;DROP TABLE users--')
        self.assertEqual(status, 400)
        self.assertEqual(len(self.dbrows('SELECT * FROM users')), 3)

    def test_undocumented_leading_underscore_params_are_ignored(self):
        # A cache-buster must not be mistaken for a column filter.
        status, data = self.get_json('/api/users/rows/?_=1234')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['rows']), 3)

    def test_bad_pagination_values_are_rejected(self):
        for query in ('limit=abc', 'limit=0', 'offset=-1', 'offset=x'):
            status, _ = self.get_json(f'/api/users/rows/?{query}')
            self.assertEqual(status, 400, query)

    def test_blob_values_are_base64(self):
        _, data = self.get_json('/api/blobs/rows/')
        self.assertEqual(data['rows'][0]['id'], {'b64': 'AP8='})

    def test_table_without_a_key_has_no_row_keys(self):
        _, data = self.get_json('/api/nopk/rows/')
        self.assertEqual(data['rows'][0]['a'], 'no-pk-row')
        self.assertNotIn('_key', data['rows'][0])

    def test_view_is_readable(self):
        status, data = self.get_json('/api/v_users/rows/')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['rows']), 3)


class TestApiRowDetail(ApiTestCase):
    def test_fetch_one_row_by_key(self):
        _, listing = self.get_json('/api/users/rows/?username=huey')
        key = listing['rows'][0]['_key']
        status, data = self.get_json(f'/api/users/rows/{key}/')
        self.assertEqual(status, 200)
        self.assertEqual(data['row'], {'id': 1, 'username': 'huey'})

    def test_key_survives_a_round_trip_through_the_url(self):
        _, listing = self.get_json('/api/users/rows/?username=zaizee')
        key = listing['rows'][0]['_key']
        self.assertNotIn('/', key)
        self.assertNotIn('+', key)
        status, _ = self.get_json(f'/api/users/rows/{key}/')
        self.assertEqual(status, 200)

    def test_malformed_key_is_a_400(self):
        status, _ = self.get_json('/api/users/rows/not-base64!!/')
        self.assertEqual(status, 400)

    def test_a_key_holding_a_list_is_a_400(self):
        # Peewee reads a list as a row-value comparison, (id) = (1, 2), which is
        # not a row: a key addresses exactly one. Hand-built, since key_encode
        # would stringify the list.
        for values in ([[1]], [[1, 2]]):
            token = base64.urlsafe_b64encode(json.dumps(values).encode()).decode()
            status, _ = self.patch_json(f'/api/users/rows/{token}/', {'username': 'nope'})
            self.assertEqual(status, 400, values)
            status, _ = self.delete_json(f'/api/users/rows/{token}/')
            self.assertEqual(status, 400, values)
        self.assertEqual(len(self.dbrows('SELECT * FROM users')), 3)

    def test_a_short_composite_key_is_a_400(self):
        # Two rows share a='US': a partial key would be an under-constrained
        # WHERE and take both, so it has to be refused rather than shortened.
        self.client.post('/api/comp/rows/', json={'a': 'US', 'b': 'second', 'label': 'second-row'})
        token = sw.key_encode(['US'])
        status, _ = self.patch_json(f'/api/comp/rows/{token}/', {'label': 'nope'})
        self.assertEqual(status, 400)
        status, _ = self.delete_json(f'/api/comp/rows/{token}/')
        self.assertEqual(status, 400)
        self.assertEqual(self.dbrows('SELECT a, b FROM comp ORDER BY b'), [('US', 'A:::B'), ('US', 'second')])

    def test_well_formed_key_with_no_row_is_a_404(self):
        status, data = self.get_json(f'/api/users/rows/{sw.key_encode([999])}/')
        self.assertEqual(status, 404)
        self.assertIn('error', data)

    def test_composite_key_addressing(self):
        _, listing = self.get_json('/api/oddpk/rows/')
        keys = [r['_key'] for r in listing['rows']]
        self.assertEqual(len(keys), 2)
        seen = set()
        for key in keys:
            status, data = self.get_json(f'/api/oddpk/rows/{key}/')
            self.assertEqual(status, 200)
            seen.add(data['row']['val'])
        self.assertEqual(seen, {'odd-row', 'same-grp'})

    def test_blob_key_addressing(self):
        _, listing = self.get_json('/api/blobs/rows/')
        key = listing['rows'][0]['_key']
        status, data = self.get_json(f'/api/blobs/rows/{key}/')
        self.assertEqual(status, 200)
        self.assertEqual(data['row']['note'], 'blob')


class TestInsert(ApiTestCase):
    def test_insert_one_row(self):
        status, data = self.post_json('/api/users/rows/', {'username': 'new'})
        self.assertEqual(status, 201)
        self.assertEqual(data['inserted'], 1)
        self.assertEqual(self.dbrows('SELECT username FROM users WHERE id = ?',
                                     key_decode_one(data['key'])),
                         [('new',)])

    def test_insert_returns_the_new_key(self):
        _, data = self.post_json('/api/users/rows/', {'username': 'new'})
        status, row = self.get_json(f"/api/users/rows/{data['key']}/")
        self.assertEqual(status, 200)
        self.assertEqual(row['row']['username'], 'new')

    def test_insert_batch(self):
        status, data = self.post_json('/api/users/rows/', {'rows': [{'username': 'a'}, {'username': 'b'}]})
        self.assertEqual(status, 201)
        self.assertEqual(data['inserted'], 2)
        self.assertEqual(self.dbrows('SELECT count(*) FROM users')[0][0], 5)

    def test_insert_batch_rolls_back_entirely_on_failure(self):
        status, data = self.post_json('/api/users/rows/', {'rows': [{'username': 'ok'}, {'nope': 'bad'}]})
        self.assertEqual(status, 400)
        self.assertIn('Row 1', data['error'])
        self.assertEqual(self.dbrows('SELECT count(*) FROM users')[0][0], 3)

    def test_insert_explicit_primary_key(self):
        status, data = self.post_json('/api/users/rows/', {'id': 77, 'username': 'z'})
        self.assertEqual(status, 201)
        self.assertEqual(key_decode_one(data['key']), 77)

    def test_insert_unknown_column_is_rejected(self):
        status, data = self.post_json('/api/users/rows/', {'nope': 1})
        self.assertEqual(status, 400)
        self.assertIn('columns', data)

    def test_insert_null_into_not_null_column_is_rejected(self):
        status, data = self.post_json('/api/oddpk/rows/', {'user id': 9, 'grp': None})
        self.assertEqual(status, 400)
        self.assertEqual(data['column'], 'grp')

    def test_insert_empty_object_is_rejected(self):
        status, _ = self.post_json('/api/users/rows/', {})
        self.assertEqual(status, 400)

    def test_insert_into_unknown_table_is_a_404(self):
        status, _ = self.post_json('/api/nope/rows/', {'a': 1})
        self.assertEqual(status, 404)

    def test_insert_into_a_table_without_a_key(self):
        status, data = self.post_json('/api/nopk/rows/', {'a': 'second'})
        self.assertEqual(status, 201)
        self.assertEqual(data['inserted'], 1)
        self.assertNotIn('key', data)

    def test_insert_blob_via_base64(self):
        status, _ = self.post_json('/api/nopk/rows/', {'a': 'x'})
        self.assertEqual(status, 201)
        _, data = self.get_json('/api/blobs/rows/')
        self.assertEqual(data['rows'][0]['id'], {'b64': 'AP8='})


class TestUpdate(ApiTestCase):
    def key_for(self, username):
        _, listing = self.get_json(f'/api/users/rows/?username={username}')
        return listing['rows'][0]['_key']

    def test_update_a_column(self):
        status, data = self.patch_json(f"/api/users/rows/{self.key_for('huey')}/", {'username': 'huey2'})
        self.assertEqual(status, 200)
        self.assertEqual(data['updated'], 1)
        self.assertEqual(self.dbrows('SELECT username FROM users WHERE id = 1'), [('huey2',)])

    def test_update_reports_zero_for_a_stale_key(self):
        status, _ = self.patch_json(f'/api/users/rows/{sw.key_encode([999])}/', {'username': 'x'})
        self.assertEqual(status, 404)

    def test_an_object_value_is_rejected(self):
        # str() would write Python's repr of it and call that a value.
        status, _ = self.patch_json(f"/api/users/rows/{self.key_for('huey')}/",
                                    {'username': {'a': 1}})
        self.assertEqual(status, 400)
        self.assertEqual(self.dbrows('SELECT username FROM users WHERE id = 1'), [('huey',)])

    def test_a_key_change_that_collides_changes_nothing(self):
        # id 2 is taken: the update has to fail whole and leave both rows alone.
        status, _ = self.patch_json(f"/api/users/rows/{self.key_for('huey')}/", {'id': 2})
        self.assertEqual(status, 400)
        self.assertEqual(self.dbrows('SELECT id, username FROM users ORDER BY id'),
                         [(1, 'huey'), (2, 'mickey'), (3, 'zaizee')])

    def test_update_unknown_column_is_rejected(self):
        status, data = self.patch_json(f"/api/users/rows/{self.key_for('huey')}/", {'nope': 1})
        self.assertEqual(status, 400)
        self.assertIn('columns', data)

    def test_update_with_no_columns_is_rejected(self):
        status, _ = self.patch_json(f"/api/users/rows/{self.key_for('huey')}/", {})
        self.assertEqual(status, 400)

    def test_update_at_a_table_without_a_key_is_refused(self):
        status, data = self.patch_json(f'/api/nopk/rows/{sw.key_encode([1])}/', {'a': 'x'})
        self.assertEqual(status, 400)
        self.assertIn('/api/query', data['error'])

    def test_update_a_composite_key_row(self):
        _, listing = self.get_json('/api/oddpk/rows/?val=odd-row')
        key = listing['rows'][0]['_key']
        status, data = self.patch_json(f'/api/oddpk/rows/{key}/', {'val': 'changed'})
        self.assertEqual(status, 200)
        self.assertEqual(data['updated'], 1)
        self.assertEqual(self.dbrows('SELECT val FROM oddpk WHERE grp = ?', 'a'), [('changed',), ('same-grp',)])


class TestDelete(ApiTestCase):
    def test_delete_a_row(self):
        _, listing = self.get_json('/api/users/rows/?username=zaizee')
        key = listing['rows'][0]['_key']
        status, data = self.delete_json(f'/api/users/rows/{key}/')
        self.assertEqual(status, 200)
        self.assertEqual(data['deleted'], 1)
        self.assertEqual(self.dbrows('SELECT count(*) FROM users')[0][0], 2)
        status, _ = self.get_json(f'/api/users/rows/{key}/')
        self.assertEqual(status, 404)

    def test_delete_at_a_table_without_a_key_is_refused(self):
        status, _ = self.delete_json(f'/api/nopk/rows/{sw.key_encode([1])}/')
        self.assertEqual(status, 400)

    def test_delete_with_a_malformed_key_is_a_400(self):
        status, _ = self.delete_json('/api/users/rows/!!!!/')
        self.assertEqual(status, 400)


class TestQuery(ApiTestCase):
    def test_select(self):
        status, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM users ORDER BY id'})
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'rows')
        self.assertEqual(data['columns'], ['id', 'username'])
        self.assertEqual(len(data['rows']), 3)
        self.assertEqual(data['sql'], 'SELECT * FROM users ORDER BY id')

    def test_positional_parameters(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT username FROM users WHERE id = ?', 'params': [2]})
        self.assertEqual(data['rows'], [{'username': 'mickey'}])

    def test_named_parameters(self):
        _, data = self.post_json('/api/query/',
                                 {'sql': 'SELECT username FROM users '
                                         'WHERE id = :id', 'params': {'id': 3}})
        self.assertEqual(data['rows'], [{'username': 'zaizee'}])

    def test_dml_reports_affected_rows(self):
        status, data = self.post_json('/api/query/', {'sql': "UPDATE users SET username = 'x'"})
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'affected')
        self.assertEqual(data['affected'], 3)

    def test_ddl_is_reported_as_affected(self):
        status, data = self.post_json('/api/query/', {'sql': 'CREATE TABLE t (a TEXT)'})
        self.assertEqual(status, 200)
        self.assertEqual(data['kind'], 'affected')

    def test_error_is_a_400_with_the_message(self):
        status, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM nope'})
        self.assertEqual(status, 400)
        self.assertIn('no such table', data['error'])
        self.assertEqual(data['sql'], 'SELECT * FROM nope')

    def test_bad_parameters_are_a_400(self):
        status, _ = self.post_json('/api/query/', {'sql': 'SELECT ?', 'params': 'nope'})
        self.assertEqual(status, 400)

    def test_missing_sql_is_a_400(self):
        for payload in ({}, {'sql': ''}, {'sql': '   '}):
            status, _ = self.post_json('/api/query/', payload)
            self.assertEqual(status, 400, payload)

    def test_non_json_body_is_a_400(self):
        status, _ = self.post_json('/api/query/', [])
        self.assertEqual(status, 400)

    def test_missing_content_type_is_a_400(self):
        response = self.client.post('/api/query/', data='sql=SELECT 1')
        self.assertEqual(response.status_code, 400)

    def test_results_are_paginated(self):
        status, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM users'})
        self.assertEqual(len(data['rows']), 3)

        response = self.client.post('/api/query/?limit=1', json={'sql': 'SELECT * FROM users'})
        data = response.get_json()
        self.assertEqual(len(data['rows']), 1)
        self.assertTrue(data['has_next'])

    def test_a_query_can_be_paged_by_offset(self):
        # Every page request used to come back as page one, so a result longer
        # than the limit could only ever show its first page.
        sql = 'SELECT * FROM users ORDER BY id'
        seen = []
        offset = 0
        for _ in range(5):  # more turns than there are pages, for the end check
            response = self.client.post(f'/api/query/?limit=1&offset={offset}', json={'sql': sql})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            seen.extend(row['id'] for row in data['rows'])
            self.assertEqual(data['offset'], offset)
            self.assertEqual(data['limit'], 1)
            if data['next_offset'] is None:
                self.assertFalse(data['has_next'])
                break
            self.assertEqual(data['next_offset'], offset + 1)
            offset = data['next_offset']
        self.assertEqual(seen, [1, 2, 3])

    def test_the_last_page_reports_no_next_offset(self):
        response = self.client.post('/api/query/?limit=2&offset=2', json={'sql': 'SELECT * FROM users ORDER BY id'})
        data = response.get_json()
        self.assertEqual([row['id'] for row in data['rows']], [3])
        self.assertFalse(data['has_next'])
        self.assertIsNone(data['next_offset'])

    def test_a_full_page_that_ends_the_table_has_no_next(self):
        # Three rows asked for, three rows there: whether a next page exists has
        # to come from the extra row the wrapper asks for, not from the page
        # being full, or the console offers a page that turns out to be empty.
        response = self.client.post('/api/query/?limit=3', json={'sql': 'SELECT * FROM users'})
        data = response.get_json()
        self.assertEqual(len(data['rows']), 3)
        self.assertFalse(data['has_next'])
        self.assertIsNone(data['next_offset'])

    def test_an_offset_past_the_end_is_empty_not_an_error(self):
        response = self.client.post('/api/query/?limit=2&offset=99', json={'sql': 'SELECT * FROM users ORDER BY id'})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['rows'], [])
        self.assertFalse(data['has_next'])
        self.assertEqual(data['offset'], 99)

    def test_a_negative_offset_is_a_400(self):
        response = self.client.post('/api/query/?offset=-1', json={'sql': 'SELECT * FROM users'})
        self.assertEqual(response.status_code, 400)

    def test_affected_results_carry_no_paging(self):
        _, data = self.post_json('/api/query/', {'sql': "UPDATE users SET username = 'x'"})
        self.assertEqual(data['kind'], 'affected')
        self.assertNotIn('next_offset', data)
        self.assertNotIn('offset', data)

    def test_run_answers_both_page_and_offset(self):
        # sqlite_web.py's query box counts pages, the JSON API counts rows; both
        # spellings have to land on the same rows.
        dataset = list(sw.datasets.values())[0]
        sql = 'SELECT id FROM users ORDER BY id'
        by_page = api._run(dataset, sql, page=2, page_size=1)
        by_offset = api._run(dataset, sql, page_size=1, offset=1)
        self.assertEqual([row[0] for row in by_page.rows], [2])
        self.assertEqual([row[0] for row in by_offset.rows], [2])

    def test_a_query_says_which_table_a_delete_would_use(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM users'})
        self.assertEqual(data['delete_target'], 'users')

    def test_a_comment_after_the_table_does_not_hide_it(self):
        sql = 'SELECT * FROM users -- every one of them\nWHERE id > 1'
        _, data = self.post_json('/api/query/', {'sql': sql})
        self.assertEqual(data['delete_target'], 'users')

    def test_rows_come_back_with_keys_the_row_endpoint_accepts(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM users'})
        status, row = self.get_json(f"/api/users/rows/{data['rows'][0]['_key']}/")
        self.assertEqual(status, 200)
        self.assertEqual(row['row']['username'], 'huey')

    def test_a_composite_key_comes_back_as_one_token(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM oddpk'})
        status, _ = self.get_json(f"/api/oddpk/rows/{data['rows'][0]['_key']}/")
        self.assertEqual(status, 200)

    def test_a_projection_without_the_key_has_no_row_keys(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT username FROM users'})
        self.assertEqual(data['delete_target'], 'users')
        self.assertNotIn('_key', data['rows'][0])

    def test_a_join_has_no_delete_target(self):
        sql = 'SELECT * FROM child JOIN parent ON child.id = parent.id'
        _, data = self.post_json('/api/query/', {'sql': sql})
        self.assertIsNone(data['delete_target'])
        self.assertNotIn('_key', data['rows'][0])

    def test_a_table_without_a_key_has_no_delete_target(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM nopk'})
        self.assertIsNone(data['delete_target'])

    def test_a_union_has_no_delete_target(self):
        # Rows from either side, so neither table owns them.
        sql = 'SELECT id FROM users UNION SELECT id FROM parent'
        _, data = self.post_json('/api/query/', {'sql': sql})
        self.assertIsNone(data['delete_target'])

    def test_a_quoted_string_does_not_hide_the_table(self):
        sql = "SELECT 'from' AS note FROM users"
        _, data = self.post_json('/api/query/', {'sql': sql})
        self.assertEqual(data['delete_target'], 'users')

    def test_a_subquery_does_not_hide_the_table(self):
        sql = 'SELECT (SELECT count(*) FROM comp) AS n FROM users'
        _, data = self.post_json('/api/query/', {'sql': sql})
        self.assertEqual(data['delete_target'], 'users')

    def test_a_qualified_table_name_is_the_table(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT * FROM main.users'})
        self.assertEqual(data['delete_target'], 'users')

    def test_an_aliased_table_has_no_delete_target(self):
        # Conservative on purpose: two names in the FROM region are not read.
        for sql in ('SELECT * FROM users u', 'SELECT * FROM "users" "comp"'):
            _, data = self.post_json('/api/query/', {'sql': sql})
            self.assertIsNone(data['delete_target'], sql)

    def test_blobs_come_back_base64_encoded(self):
        _, data = self.post_json('/api/query/', {'sql': 'SELECT id FROM blobs'})
        self.assertEqual(data['rows'][0]['id'], {'b64': 'AP8='})

    def test_bound_parameters_with_pagination(self):
        _, data = self.post_json('/api/query/?limit=1', {'sql': 'SELECT * FROM users WHERE id > ?', 'params': [0]})
        self.assertEqual(len(data['rows']), 1)
        self.assertTrue(data['has_next'])


class TestScript(ApiTestCase):
    def test_runs_each_statement(self):
        status, data = self.post_json(
            '/api/script/',
            {'sql': "INSERT INTO users (username) VALUES ('new');"
                    ' SELECT count(*) AS n FROM users;'})
        self.assertEqual(status, 200)
        self.assertFalse(data['failed'])
        self.assertEqual(data['total_statements'], 2)
        self.assertEqual(data['results'][0]['kind'], 'affected')
        self.assertEqual(data['results'][1]['rows'], [{'n': 4}])

    def test_stops_at_the_first_error(self):
        status, data = self.post_json('/api/script/', {'sql': 'SELECT * FROM nope; SELECT 1;'})
        self.assertEqual(status, 200)
        self.assertTrue(data['failed'])
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['kind'], 'error')
        self.assertIn('no such table', data['results'][0]['error'])

    def test_empty_script_is_a_400(self):
        status, _ = self.post_json('/api/script/', {'sql': '   '})
        self.assertEqual(status, 400)


class TestDatasetSelection(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.other_path = os.path.join(self.tmp, 'other.db')
        conn = sqlite3.connect(self.other_path)
        conn.executescript('CREATE TABLE thing (id INTEGER PRIMARY KEY);' "INSERT INTO thing VALUES (5);")
        conn.commit()
        conn.close()
        sw.initialize_app([self.other_path])

    def test_both_databases_are_listed(self):
        _, data = self.get_json('/api/datasets/')
        self.assertEqual(len(data['datasets']), 2)

    def test_default_dataset_is_the_session_one(self):
        # No cookie and no ?dataset=, so get_dataset falls back to the first
        # loaded database -- app.db.
        status, tables = self.get_json('/api/tables/')
        self.assertEqual(status, 200)
        names = {t['name'] for t in tables['tables']}
        self.assertIn('users', names)
        self.assertNotIn('thing', names)

    def test_select_by_full_path(self):
        status, data = self.get_json(f'/api/tables/?dataset={self.other_path}')
        self.assertEqual(status, 200)
        self.assertEqual([t['name'] for t in data['tables']], ['thing'])

    def test_select_by_basename(self):
        for name in ('other.db', 'other'):
            status, data = self.get_json(f'/api/tables/?dataset={name}')
            self.assertEqual(status, 200, name)
            self.assertEqual([t['name'] for t in data['tables']], ['thing'])

    def test_select_by_header(self):
        response = self.client.get('/api/tables/', headers={'X-Dataset': 'other.db'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t['name'] for t in response.get_json()['tables']], ['thing'])

    def test_query_runs_against_the_selected_dataset(self):
        status, data = self.post_json('/api/query/?dataset=other.db', {'sql': 'SELECT * FROM thing'})
        self.assertEqual(status, 200)
        self.assertEqual(data['rows'][0]['id'], 5)

    def test_unknown_dataset_is_a_404(self):
        status, data = self.get_json('/api/tables/?dataset=nope')
        self.assertEqual(status, 404)
        self.assertEqual(len(data['available']), 2)

    def test_every_path_the_api_reports_is_usable_as_a_selector(self):
        # Including the 8.3 short form tempfile.mkdtemp produces on Windows.
        _, data = self.get_json('/api/datasets/')
        for entry in data['datasets']:
            for name in (entry['key'], entry['basename']):
                status, _ = self.get_json(f'/api/tables/?dataset={name}')
                self.assertEqual(status, 200, name)


class TestApiReadOnly(ApiTestCase):
    def setUp(self):
        super().setUp()
        sw.datasets.clear()
        sw.initialize_app([self.db_path], read_only=True)
        sw.app.config['TESTING'] = True
        self.client = sw.app.test_client()

    def test_reads_still_work(self):
        status, data = self.get_json('/api/users/rows/')
        self.assertEqual(status, 200)
        self.assertTrue(data['is_readonly'])

    def test_writes_are_refused(self):
        status, data = self.post_json('/api/users/rows/', {'username': 'x'})
        self.assertEqual(status, 403)
        self.assertIn('read-only', data['error'])

        status, _ = self.post_json('/api/query/', {'sql': "DELETE FROM users"})
        self.assertEqual(status, 403)

    def test_scripts_that_write_are_refused(self):
        status, data = self.post_json('/api/script/', {'sql': 'DELETE FROM users'})
        self.assertEqual(status, 403)
        self.assertIn('read-only', data['error'])

    def test_queries_still_work(self):
        status, data = self.post_json('/api/query/', {'sql': 'SELECT count(*) AS n FROM users'})
        self.assertEqual(status, 200)
        self.assertEqual(data['rows'], [{'n': 3}])


class TestCrossSitePolicy(ApiTestCase):
    def test_cross_site_post_is_refused(self):
        # Inherited from the HTML app: a browser labels a cross-site form post
        # this way, and the API must not be a way around that.
        response = self.client.post('/api/query/', json={'sql': 'SELECT 1'}, headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(response.status_code, 403)

    def test_a_client_that_omits_the_header_is_allowed(self):
        status, _ = self.post_json('/api/query/', {'sql': 'SELECT 1'})
        self.assertEqual(status, 200)


class TestRegistration(unittest.TestCase):
    def test_disabled_means_no_blueprint(self):
        app = Flask(__name__)
        api.register(app, enabled=False)
        self.assertNotIn('api', app.blueprints)

    def test_enabled_registers_once(self):
        app = Flask(__name__)
        api.register(app, enabled=True)
        self.assertIn('api', app.blueprints)
        before = {r.rule for r in app.url_map.iter_rules()}
        api.register(app, enabled=True)
        self.assertEqual({r.rule for r in app.url_map.iter_rules()}, before)

    def test_does_not_register_a_static_route(self):
        # A rule on /static/ from this blueprint would shadow the HTML app's.
        app = Flask(__name__)
        api.register(app)
        self.assertNotIn('api.static', app.view_functions)

    def test_default_is_enabled(self):
        app = Flask(__name__)
        api.register(app)
        self.assertIn('api', app.blueprints)


class TestPasswordAuth(BaseAppTestCase):
    """-P must protect the JSON API without breaking the Vue shell.

    The app is a module-level singleton and install_auth_handler appends a
    before_request hook to it, so the hook table is snapshotted and restored in
    tearDown -- otherwise the hook would follow every later test around.
    """

    def setUp(self):
        super().setUp()
        self._hooks = {k: list(v) for k, v in sw.app.before_request_funcs.items()}
        self.addCleanup(self._restore_hooks)
        # Flask refuses to add a before_request hook once the app has served a
        # request, and every earlier test here has. In production
        # install_auth_handler runs from initialize_app, before serving.
        sw.app._got_first_request = False
        sw.install_auth_handler('secret')
        self.client = sw.app.test_client()

    def _restore_hooks(self):
        sw.app.before_request_funcs.clear()
        sw.app.before_request_funcs.update(self._hooks)
        sw.app.config.pop('PASSWORD', None)

    def test_api_answers_401_instead_of_redirecting(self):
        response = self.client.get('/api/tables/')
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response.get_json())

    def test_writes_are_gated_too(self):
        response = self.client.post('/api/users/rows/', json={'username': 'x'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.dbrows('SELECT COUNT(*) FROM users')[0][0], 3)

    def test_shell_redirects_to_the_login_form(self):
        response = self.client.get('/app/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.headers['Location'])

    def test_assets_are_served_without_a_session(self):
        # The shell cannot render without them, and the login page has none of
        # the names it asks for.
        shell = self.client.get('/login/').status_code
        self.assertEqual(shell, 200)
        response = self.client.get('/app/assets/nope.js')
        self.assertEqual(response.status_code, 404)

    def test_logging_in_opens_the_api(self):
        self.assertEqual(self.client.get('/api/tables/').status_code, 401)
        self.client.post('/login/', data={'password': 'secret'})
        self.assertEqual(self.client.get('/api/tables/').status_code, 200)

    def test_wrong_password_leaves_it_shut(self):
        self.client.post('/login/', data={'password': 'nope'})
        self.assertEqual(self.client.get('/api/tables/').status_code, 401)


class TestVueApp(unittest.TestCase):
    """The shell's asset URLs have to stay relative, and the assets have to be
    served from under /app/ so -u/--url-prefix moves them along with it."""

    def setUp(self):
        if not vue.is_built():
            self.skipTest('frontend has not been built (cd frontend && npm run build)')
        self.app = Flask(__name__)
        vue.register(self.app)
        self.client = self.app.test_client()

    def test_disabled_means_no_blueprint(self):
        app = Flask(__name__)
        vue.register(app, enabled=False)
        self.assertNotIn('vue', app.blueprints)

    def test_enabled_registers_once(self):
        self.assertIn('vue', self.app.blueprints)
        before = {r.rule for r in self.app.url_map.iter_rules()}
        vue.register(self.app, enabled=True)
        self.assertEqual({r.rule for r in self.app.url_map.iter_rules()}, before)

    def test_does_not_register_a_static_route(self):
        # The HTML app owns /static/; a rule on that path would shadow it and
        # break the old frontend's assets.
        self.assertNotIn('vue.static', self.app.view_functions)
        self.assertFalse(vue.bp.has_static_folder)

    def test_shell_is_served(self):
        response = self.client.get('/app/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('assets/index-', response.get_data(as_text=True))

    def test_assets_are_relative_and_resolve(self):
        # Vite's base: './' is what makes -u/--url-prefix survivable; an
        # absolute /static/vue/... would silently break it.
        shell = self.client.get('/app/').get_data(as_text=True)
        assets = re.findall(r'(?:src|href)="(\./assets/[^"]+)"', shell)
        self.assertGreaterEqual(len(assets), 2, shell)
        for rel in assets:
            response = self.client.get('/app/' + rel[2:])
            self.assertEqual(response.status_code, 200, rel)
            self.assertGreater(len(response.get_data()), 500, rel)

    def test_unknown_asset_is_a_404(self):
        self.assertEqual(self.client.get('/app/assets/nope.js').status_code, 404)

    def test_unbuilt_checkout_does_not_mount_the_routes(self):
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.object(vue, 'BUILT_DIR', empty):
                app = Flask(__name__)
                vue.register(app)
        self.assertNotIn('vue', app.blueprints)


class TestVueOnTheRealApp(BaseAppTestCase):
    def setUp(self):
        super().setUp()
        if not vue.is_built():
            self.skipTest('frontend has not been built (cd frontend && npm run build)')

    def test_app_is_mounted(self):
        response = self.client.get('/app/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('assets/index-', response.get_data(as_text=True))

    def test_api_is_mounted(self):
        self.assertEqual(self.client.get('/api/tables/').status_code, 200)

    def test_root_serves_the_vue_app_by_default(self):
        # BaseAppTestCase pins USE_OLD_UI so tests.py can keep testing the
        # original index; the dev branch's default is the other way round.
        sw.app.config.pop('USE_OLD_UI', None)
        self.addCleanup(sw.app.config.__setitem__, 'USE_OLD_UI', True)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/app/'), response.headers['Location'])

    def test_use_old_ui_puts_the_original_index_back(self):
        sw.app.config['USE_OLD_UI'] = True
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/users/').status_code, 200)
        self.assertEqual(self.client.get('/app/').status_code, 200)


class TestCliFlag(unittest.TestCase):
    def test_the_vue_app_is_the_default(self):
        options, _ = sw.get_option_parser().parse_args(['some.db'])
        self.assertFalse(options.use_old_ui)

    def test_use_old_ui(self):
        options, _ = sw.get_option_parser().parse_args(['--use-old-ui', 'some.db'])
        self.assertTrue(options.use_old_ui)
        options, _ = sw.get_option_parser().parse_args(['-O', 'some.db'])
        self.assertTrue(options.use_old_ui)

    def test_the_removed_flags_are_gone(self):
        # --no-api and --old-ui were folded into --use-old-ui. optparse has to
        # reject them: accepting one and quietly doing nothing would be worse
        # than an error for a flag that used to switch off a whole API.
        for flag in ('-A', '--no-api', '--old-ui'):
            with mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit, msg=flag):
                    sw.get_option_parser().parse_args([flag, 'some.db'])


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _kill(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestRunAsMain(unittest.TestCase):
    """
    ``python -m sqlite_web`` has to serve the same app as the console script.

    That is what __main__.py exists for, and it fails quietly when __main__.py
    imports the package rather than the module defining main(): __init__.py is
    empty, most of the JSON API stays healthy, and only the endpoints that
    enumerate the datasets registry go empty -- the Vue app renders blank while
    /api/tables/ and every row endpoint answer perfectly.

    A subprocess is the only honest way to test it. In-process the module is
    imported under the name the app was built from, which is exactly the
    condition under which the bug cannot occur.
    """

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = os.path.join(tmp, 'app.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute("INSERT INTO t (name) VALUES ('hello')")
        conn.commit()
        conn.close()
        self.port = _free_port()
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(sw.__file__)))

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def start(self):
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'sqlite_web', '-x', '-p', str(self.port), self.db],
            cwd=self.root, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(_kill, proc)
        for _ in range(160):
            if proc.poll() is not None:
                log.seek(0)
                output = log.read().decode('utf-8', 'replace')[:800]
                self.fail(f'server exited: {output}')
            try:
                urllib.request.urlopen(self.url('/api/datasets/'), timeout=2).read()
                return
            except urllib.error.HTTPError:
                return
            except Exception:
                time.sleep(0.25)
        self.fail(f'server never came up on port {self.port}')

    def fetch(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as fh:
            return json.loads(fh.read())

    def test_the_registry_is_not_empty(self):
        self.start()
        payload = self.fetch('/api/datasets/')
        self.assertEqual(len(payload['datasets']), 1, payload)

    def test_a_dataset_can_be_addressed_by_name(self):
        # The app addresses a database by name on every request after the
        # first, through the registry rather than through the HTML session.
        self.start()
        key = self.fetch('/api/datasets/')['datasets'][0]['key']
        tables = self.fetch('/api/tables/?dataset=' + urllib.parse.quote(key))
        self.assertEqual([t['name'] for t in tables['tables']], ['t'])

def key_decode_one(token):
    return key_decode(token)[0]


if __name__ == '__main__':
    unittest.main()
