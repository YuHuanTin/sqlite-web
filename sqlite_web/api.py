"""
The JSON API behind the Vue app.

Two naming conventions are in play, matching the two halves of the HTML UI.
``/api/query`` and ``/api/script`` report raw SQL **column** names, exactly as
the query box does. ``/api/<table>/...`` uses peewee **field** names, exactly as
the content tab does -- and reflection sanitizes a column name into a field
name, so ``"user id"`` becomes ``user_id``. ``GET /api/<table>/schema`` reports
both so a client can see the mapping.
"""
import base64
import os
import re

from flask import Blueprint, current_app, jsonify, request

from peewee import BlobField
from peewee import CompositeKey
from peewee import DatabaseError
from peewee import IntegerField

from sqlite_web import sqlite_web as sw
from sqlite_web.executor import _enc, Result, split_statements, wrap


bp = Blueprint('api', __name__, url_prefix='/api', static_folder=None)

DEFAULT_LIMIT = 100
MAX_LIMIT = 10000

# Steer the request rather than filter on a column, so a table with a column
# named "order" cannot be filtered on it.
RESERVED_PARAMS = frozenset(('dataset', 'limit', 'offset', 'order'))

ENDPOINTS = (
    ('GET', '/api/', 'This index.'),
    ('GET', '/api/datasets/', 'Loaded databases.'),
    ('GET', '/api/tables/', 'Tables and views in the active database.'),
    ('GET', '/api/<table>/schema/', 'Columns, indexes, triggers, foreign keys.'),
    ('GET', '/api/<table>/rows/', 'Paginated rows. Filters are bare query '
     'parameters, e.g. ?username=huey. Control knobs: limit, offset, order, '
     'dataset.'),
    ('GET', '/api/<table>/rows/<key>/', 'A single row, addressed by its key.'),
    ('POST', '/api/<table>/rows/', 'Insert {"rows": [...]} or a bare {..}.'),
    ('PATCH', '/api/<table>/rows/<key>/', 'Update the given columns of one row.'),
    ('DELETE', '/api/<table>/rows/<key>/', 'Delete one row.'),
    ('POST', '/api/query/', 'Run one statement: {"sql": ..., "params": ...}.'),
    ('POST', '/api/script/', 'Run a multi-statement script: {"sql": ...}.'),
)


class ApiError(Exception):
    """Raised anywhere below, rendered as a JSON error body."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


@bp.errorhandler(ApiError)
def _render_api_error(exc):
    payload = {'error': exc.message}
    payload.update(exc.extra)
    return jsonify(payload), exc.status


def _dataset():
    # Falling back to the session the HTML UI keeps is only meaningful for a
    # cookie-carrying client: a plain HTTP client has no cookie jar, so silence
    # here would mean every request hitting whichever database loaded first.
    requested = request.args.get('dataset') or request.headers.get('X-Dataset')
    if not requested:
        return sw.get_dataset()

    matches = {}
    for key, dataset in sw.datasets.items():
        for name in (key, dataset.filename, dataset.basename, dataset.basename.rsplit('.', 1)[0]):
            matches.setdefault(_normpath(name), dataset)

    try:
        return matches[_normpath(requested)]
    except KeyError:
        raise ApiError(f'Unknown dataset: {requested!r}', 404, available=sorted(sw.datasets))


def _normpath(path):
    # tempfile.mkdtemp hands back an 8.3 short path on Windows ("YUHUAN~1")
    # while the dataset is keyed on the long one. realpath is what expands the
    # short form; abspath does not.
    return os.path.normcase(os.path.realpath(path))


def _table(table):
    dataset = _dataset()
    dataset.ensure_cache()
    if table not in dataset.cached_tables():
        raise ApiError(f'Unknown table or view: {table!r}', 404, available=sorted(dataset.cached_tables()))
    return dataset, dataset[table].model_class


def _writable(dataset):
    if dataset.is_readonly:
        raise ApiError('Database is open in read-only mode.', 403)


def _addressable(dataset, table):
    # Both row endpoints need a key to say which row they mean.
    if not dataset.cached_has_usable_pk(table):
        raise ApiError('Table has no usable primary key; address it with /api/query instead.', 400)


def _body():
    data = request.get_json(silent=True)
    if data is None:
        raise ApiError('Expected a JSON request body. Send Content-Type: application/json.')
    return data


def _int_arg(name, default, least):
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(f'{name!r} must be an integer.')
    if value < least:
        raise ApiError(f'{name!r} must be at least {least}.')
    return value


def _limit():
    return min(_int_arg('limit', DEFAULT_LIMIT, 1), MAX_LIMIT)


def _offset():
    return _int_arg('offset', 0, 0)


def _field(model, name):
    """Resolve a column name or a peewee field name to the field, or None."""
    return model._meta.columns.get(name) or model._meta.fields.get(name)


def _coerce(field, value):
    if value is None:
        # A native JSON null is SQL NULL; the form layer spells that "null".
        if not field.null:
            return None, 'Column does not allow NULL values.'
        return None, None

    if isinstance(field, BlobField):
        # The {"b64": ...} shape executor._enc emits, so a value read from the
        # API round-trips when written back.
        if isinstance(value, dict) and 'b64' in value:
            try:
                raw = base64.b64decode(value['b64'], validate=True)
            except Exception:
                return value, 'Value must be valid base64-encoded binary data.'
            if current_app.config['BLOB_AS_BASE64']:
                value = value['b64']
            else:
                value = raw.hex()
        elif not isinstance(value, str):
            return value, ('Value must be a base64 string, a hex string, or {"b64": ...}.')

    if isinstance(value, (list, dict)):
        # str() would write Python's repr of it and call that a value.
        return value, 'Value must be a string, not an object or array.'

    if isinstance(value, bool):
        value = 'true' if value else 'false'
    elif not isinstance(value, str):
        value = str(value)

    return sw.minimal_validate_field(field, value)


def _prepare(model, data):
    """Turn a JSON object into a {field: value} dict for insert/update."""
    if not isinstance(data, dict):
        raise ApiError('Expected a JSON object of column names to values.')

    prepared = {}
    for name, value in data.items():
        field = _field(model, name)
        if field is None:
            raise ApiError(f'Unknown column: {name!r}', 400, columns=sorted(model._meta.columns))
        value, error = _coerce(field, value)
        if error:
            raise ApiError(f'Invalid value for {name!r}: {error}', 400, column=name)
        prepared[field] = value
    return prepared


def _run(dataset, sql, params=None, page=1, page_size=DEFAULT_LIMIT, ordering=None, offset=None):
    """
    Execute one statement and return an executor.Result.

    `page` is how the HTML query box asks (it counts pages); `offset` is how the
    JSON API asks (its callers count rows, as they do for table rows). Only one
    of them is ever passed.
    """
    if params is not None and not isinstance(params, (list, tuple, dict)):
        raise ApiError("'params' must be a list (positional) or an object " '(named).')
    params = () if params is None else params
    if offset is None:
        offset = (max(page, 1) - 1) * page_size
    else:
        offset = max(offset, 0)

    try:
        cursor = dataset.query(wrap(sql, ordering, page_size + 1, offset), params)
        paged = True
    except DatabaseError:
        try:
            cursor = dataset.query(sql, params)
            paged = False
        except Exception as exc:
            return Result('error', sql, error=str(exc))

    if cursor.description is None:
        return Result('affected', sql, affected=cursor.rowcount)

    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return Result(
        'rows',
        sql,
        columns=columns,
        rows=rows[:page_size] if paged else rows,
        has_next=paged and len(rows) > page_size)


def _encode_row(columns, row):
    # executor._enc is the one place that decides how a SQLite value becomes
    # JSON; a second copy of that rule here would let the query box and the API
    # drift apart.
    return dict(zip(columns, (_enc(value) for value in row)))


def _result_payload(result):
    if result.kind == 'error':
        return None  # The caller turns this into an ApiError via _error_from.
    payload = {'kind': result.kind, 'sql': result.statement}
    if result.kind == 'rows':
        payload['columns'] = list(result.columns)
        payload['rows'] = [_encode_row(result.columns, row) for row in result.rows]
        payload['has_next'] = bool(result.has_next)
    else:
        payload['affected'] = result.affected
    return payload


def _readonly_failure(result):
    # SQLite itself refuses the write, which is the enforcement point we want;
    # only the status code needs fixing up.
    return result.kind == 'error' and ('readonly' in result.error or 'read-only' in result.error)


def _error_from(result):
    if _readonly_failure(result):
        return ApiError('Database is open in read-only mode.', 403, sql=result.statement)
    return ApiError(result.error, 400, sql=result.statement)


def _sql_from_body():
    data = _body()
    if not isinstance(data, dict):
        raise ApiError('Expected a JSON object with a "sql" key.')
    sql = (data.get('sql') or '').strip()
    if not sql:
        raise ApiError("Missing 'sql'.")
    return sql, data.get('params')


@bp.get('/')
def api_index():
    return jsonify({
        'datasets': sorted(sw.datasets),
        'endpoints': [{'method': method, 'path': path, 'doc': doc} for method, path, doc in ENDPOINTS],
    })


@bp.get('/datasets/')
def api_datasets():
    return jsonify({'datasets': [
        {'key': key, 'basename': dataset.basename, 'is_readonly': bool(dataset.is_readonly)}
        for key, dataset in sw.datasets.items()]})


def _row_count(dataset, table):
    """How many rows a table holds, or None if it will not say.

    A view has no rows of its own and COUNT(*) runs its query, so this is the
    one call that can be genuinely expensive -- and the one that can fail, since
    a view can reference a table that is gone. Neither may take the whole table
    list down with it, hence the None.
    """
    try:
        return dataset[table].model_class.select().count()
    except Exception:
        return None


@bp.get('/tables/')
def api_tables():
    dataset = _dataset()
    dataset.ensure_cache()
    tables = sorted(dataset.cached_tables())
    return jsonify({
        'tables': [{'name': name, 'is_view': dataset.cached_is_view(name),
                    'has_usable_pk': dataset.cached_has_usable_pk(name),
                    'count': _row_count(dataset, name)}
                   for name in tables],
        'is_readonly': bool(dataset.is_readonly),
    })


@bp.get('/<table>/schema/')
def api_schema(table):
    dataset, model = _table(table)
    columns = []
    for meta in dataset.get_columns(table):
        field = model._meta.columns.get(meta.name)
        columns.append({
            # "column" is the SQL name, "field" the peewee name the row
            # endpoints speak; they differ only when reflection sanitized one.
            'column': meta.name,
            'field': field.name if field is not None else None,
            'type': meta.data_type,
            'full_type': meta.full_type,
            'null': bool(meta.null),
            'default': meta.default,
            'primary_key': bool(meta.primary_key),
            'autoincrement': bool(meta.identity),
        })

    # peewee uses a bare False to mean "this model has no primary key".
    pk = model._meta.primary_key
    if isinstance(pk, CompositeKey):
        primary_key = {'composite': True, 'fields': list(pk.field_names)}
    elif pk:
        primary_key = {'composite': False, 'field': pk.name, 'column': pk.column_name}
    else:
        primary_key = None

    def index_meta(index):
        return {'name': index.name, 'sql': index.sql,
                'columns': list(index.columns), 'unique': bool(index.unique)}

    def fk_meta(fk):
        return {'column': fk.column, 'dest_table': fk.dest_table,
                'dest_column': fk.dest_column, 'on_delete': fk.on_delete,
                'on_update': fk.on_update}

    return jsonify({
        'table': table,
        'sql': dataset.cached_table_sql(table),
        'columns': columns,
        'primary_key': primary_key,
        'has_usable_pk': dataset.cached_has_usable_pk(table),
        'is_view': dataset.cached_is_view(table),
        'is_readonly': bool(dataset.is_readonly),
        'indexes': [index_meta(i) for i in dataset.get_indexes(table)],
        'foreign_keys': [fk_meta(f) for f in dataset.get_foreign_keys(table)],
        'triggers': list(dataset.get_triggers(table)),
    })


@bp.get('/<table>/rows/')
def api_rows(table):
    dataset, model = _table(table)
    limit, offset = _limit(), _offset()

    query = model.select()
    for name, raw in request.args.items():
        if name in RESERVED_PARAMS or name.startswith('_'):
            continue
        field = _field(model, name)
        if field is None:
            raise ApiError(f'Unknown column: {name!r}', 400, columns=sorted(model._meta.columns))
        query = query.where(field == raw)

    order = request.args.get('order')
    if order:
        descending = order.startswith('-')
        name = order.lstrip('-')
        field = _field(model, name)
        if field is None:
            raise ApiError(f'Unknown column in "order": {name!r}', 400, columns=sorted(model._meta.columns))
        query = query.order_by(field.desc() if descending else field.asc())

    # One extra row is what says whether another page exists.
    rows = list(query.limit(limit + 1).offset(offset).dicts())
    has_next = len(rows) > limit
    rows = rows[:limit]

    writable = not dataset.is_readonly
    payload = [_encode_row(row.keys(), row.values()) for row in rows]

    pk = model._meta.primary_key
    usable = dataset.cached_has_usable_pk(table)
    if usable:
        for row, encoded in zip(rows, payload):
            try:
                encoded['_key'] = sw.encode_pk(row, pk)
            except Exception:
                # A column the projection did not include; not addressable.
                pass

    columns = [c.name for c in dataset.get_columns(table)]
    return jsonify({
        'table': table,
        'columns': columns,
        'rows': payload,
        'limit': limit,
        'offset': offset,
        'has_next': has_next,
        'next_offset': offset + limit if has_next else None,
        'is_readonly': bool(dataset.is_readonly),
    })


def _one_row(model, table, token):
    """Decode a row key and return (expression, model instance dict)."""
    try:
        expr = sw.decode_pk(model, token)
    except Exception:
        raise ApiError('Malformed row key.', 400)
    row = model.select().where(expr).dicts().first()
    if row is None:
        raise ApiError('No row matches that key.', 404)
    return expr, row


@bp.get('/<table>/rows/<token>/')
def api_row_detail(table, token):
    dataset, model = _table(table)
    _, row = _one_row(model, table, token)
    payload = _encode_row(list(row.keys()), list(row.values()))
    return jsonify({'table': table, 'row': payload})


@bp.post('/<table>/rows/')
def api_insert(table):
    dataset, model = _table(table)
    _writable(dataset)

    data = _body()
    if isinstance(data, dict) and 'rows' in data:
        rows = data['rows']
    else:
        rows = data
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ApiError('Expected a JSON object, or {"rows": [...]} with at least one row.')
    if len(rows) > MAX_LIMIT:
        raise ApiError(f'Too many rows in one request (max {MAX_LIMIT}).')

    prepared = []
    for index, row in enumerate(rows):
        try:
            prepared.append(_prepare(model, row))
        except ApiError as exc:
            raise ApiError(f'Row {index}: {exc.message}', exc.status, **exc.extra)
    if any(not row for row in prepared):
        raise ApiError('At least one row had no columns to insert.')

    try:
        with dataset.transaction():
            if len(prepared) == 1:
                last_id = model.insert(prepared[0]).execute()
                count = 1
            else:
                model.insert_many(prepared).execute()
                count, last_id = len(prepared), None
    except Exception as exc:
        raise ApiError(f'Insert failed: {exc}', 400)

    payload = {'inserted': count}
    # Only meaningful for a single auto-increment integer key; nothing else can
    # be reconstructed reliably from what SQLite hands back.
    pk = model._meta.primary_key
    if last_id is not None and not isinstance(pk, CompositeKey) \
            and isinstance(pk, IntegerField):
        try:
            payload['key'] = sw.key_encode([last_id])
        except Exception:
            pass
    return jsonify(payload), 201


@bp.patch('/<table>/rows/<token>/')
def api_update(table, token):
    dataset, model = _table(table)
    _writable(dataset)
    _addressable(dataset, table)

    expr, _ = _one_row(model, table, token)
    update = _prepare(model, _body())
    if not update:
        raise ApiError('No columns to update.')

    try:
        with dataset.transaction():
            count = model.update(update).where(expr).execute()
    except Exception as exc:
        raise ApiError(f'Update failed: {exc}', 400)

    return jsonify({'updated': count})


@bp.delete('/<table>/rows/<token>/')
def api_delete(table, token):
    dataset, model = _table(table)
    _writable(dataset)
    _addressable(dataset, table)

    expr, _ = _one_row(model, table, token)
    try:
        with dataset.transaction():
            count = model.delete().where(expr).execute()
    except Exception as exc:
        raise ApiError(f'Delete failed: {exc}', 400)

    return jsonify({'deleted': count})


@bp.post('/query/')
def api_query():
    dataset = _dataset()
    sql, params = _sql_from_body()
    limit, offset = _limit(), _offset()
    result = _run(dataset, sql, params=params, page_size=limit, offset=offset)
    payload = _result_payload(result)
    if payload is None:
        raise _error_from(result)
    if payload['kind'] == 'rows':
        payload['limit'] = limit
        payload['offset'] = offset
        payload['next_offset'] = offset + limit if payload['has_next'] else None
        target = _delete_target(dataset, sql)
        payload['delete_target'] = target
        if target:
            keys = _row_keys(dataset[target].model_class, list(result.columns), result.rows)
            for encoded, key in zip(payload['rows'], keys):
                encoded['_key'] = key
    return jsonify(payload)


@bp.post('/script/')
def api_script():
    dataset = _dataset()
    sql, _ = _sql_from_body()
    statements = split_statements(sql)
    if not statements:
        raise ApiError('No statements found in "sql".')

    results = []
    for statement in statements:
        result = _run(dataset, statement, page_size=_limit())
        if result.kind == 'error':
            # A read-only database is a permission problem, not a bad script:
            # it gets a status code rather than a 200 burying it in the results.
            if _readonly_failure(result):
                raise _error_from(result)
            results.append({'kind': 'error', 'sql': result.statement, 'error': result.error})
            break
        results.append(_result_payload(result))

    return jsonify({
        'results': results,
        'failed': any(r['kind'] == 'error' for r in results),
        'total_statements': len(statements),
    })


def _skip_quoted(sql, at):
    quote = sql[at]
    at += 1
    while at < len(sql):
        if sql[at] == quote:
            if sql.startswith(quote * 2, at):
                at += 2
                continue
            return at + 1
        at += 1
    return at


def _scan(sql):
    """The SQL with comments blanked out, plus its top-level words and where each sits.

    One pass, because the two halves have to agree: the masked text is what the
    table name gets sliced out of, so a comment between the name and a clause
    cannot make the name look like more than one word.
    """
    out = []
    words = []
    produced = 0
    depth = 0
    at = 0
    end = len(sql)

    def take(piece):
        nonlocal produced
        out.append(piece)
        produced += len(piece)

    while at < end:
        char = sql[at]
        if char in ('"', "'", '`'):
            stop = _skip_quoted(sql, at)
            take(sql[at:stop])
        elif char == '[':
            closing = sql.find(']', at)
            stop = end if closing < 0 else closing + 1
            take(sql[at:stop])
        elif sql.startswith('--', at):
            newline = sql.find('\n', at)
            stop = end if newline < 0 else newline
            take(' ')
        elif sql.startswith('/*', at):
            closing = sql.find('*/', at + 2)
            stop = end if closing < 0 else closing + 2
            take(' ')
        elif char == '(':
            depth += 1
            stop = at + 1
            take(char)
        elif char == ')':
            depth = max(0, depth - 1)
            stop = at + 1
            take(char)
        elif char.isalnum() or char in '_$':
            stop = at
            while stop < end and (sql[stop].isalnum() or sql[stop] in '_$'):
                stop += 1
            if depth == 0 and not char.isdigit():
                words.append((sql[at:stop].lower(), produced, produced + stop - at))
            take(sql[at:stop])
        else:
            stop = at + 1
            take(char)
        at = stop

    return ''.join(out), words


def _unquote(name):
    if len(name) > 1 and name[0] == name[-1] and name[0] in ('"', '`'):
        return name[1:-1].replace(name[0] * 2, name[0])
    if name.startswith('[') and name.endswith(']'):
        return name[1:-1]
    return name


_BARE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_$.]*$')

# Two word lists the table name has to survive: what ends its region, and what
# says its rows came from more than one table.
_CLAUSE_WORDS = ('where', 'order', 'limit', 'group', 'having', 'window', 'union', 'intersect', 'except')
_MANY_TABLES = ('join', 'union', 'intersect', 'except')


def _single_table(sql):
    """The one table a plain SELECT reads, or None.

    None for anything whose rows could have come from more than one table: a
    joined row has no single key to address it by.
    """
    text, words = _scan(sql.strip().rstrip(';').strip())
    if not words or words[0][0] != 'select':
        return None
    if {word for word, _, _ in words} & set(_MANY_TABLES):
        return None
    at_from = next((i for i, word in enumerate(words) if word[0] == 'from'), None)
    if at_from is None:
        return None
    after = words[at_from][2]
    stop = next((word[1] for word in words[at_from + 1:] if word[0] in _CLAUSE_WORDS), len(text))
    part = text[after:stop].strip()
    if len(part.split()) != 1:
        return None
    if part[0] in ('"', '`', '['):
        return _unquote(part)
    return part.split('.')[-1] if _BARE_NAME.match(part) else None


def _delete_target(dataset, sql):
    """The table whose rows this statement read, if a delete could follow it."""
    table = _single_table(sql)
    if table is None:
        return None
    dataset.ensure_cache()
    if table not in dataset.cached_tables() or not dataset.cached_has_usable_pk(table):
        return None
    return table


def _row_keys(model, columns, rows):
    """Key tokens for the rows of one statement, or [] if the projection dropped a key column.

    The row endpoints take these, so the console deletes through them instead of
    writing its own DELETE.
    """
    pk = model._meta.primary_key
    names = pk.field_names if isinstance(pk, CompositeKey) else [pk.name]
    try:
        at = [columns.index(model._meta.fields[name].column_name) for name in names]
    except ValueError:
        return []
    return [sw.encode_pk(dict(zip(names, (row[index] for index in at))), pk) for row in rows]


def register(app, enabled=True):
    """Attach the API. Called from sqlite_web.configure_app()."""
    if not enabled or 'api' in app.blueprints:
        return
    app.register_blueprint(bp)
