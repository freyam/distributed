""" This file is experimental and may disappear without warning """
from __future__ import print_function, division, absolute_import

import logging
import json
from math import log
import os
import io
import sys

from dask.imperative import Value
from tornado import gen
from toolz import merge

from .executor import default_executor
from .utils import ignoring, sync


logger = logging.getLogger(__name__)


def walk_glob(hdfs, path):
    if '*' not in path and hdfs.info(path)['kind'] == 'directory':
        return sorted([fn for fn in hdfs.walk(path) if fn[-1] != '/'])
    else:
        return sorted(hdfs.glob(path))


def get_block_locations(hdfs, filename):
    """ Get block locations from a filename or globstring """
    return [merge({'filename': fn}, block)
            for fn in walk_glob(hdfs, filename)
            for block in hdfs.get_block_locations(fn)]


def read_block_from_hdfs(filename, offset, length, host=None, port=None,
        delimiter=None):
    from hdfs3 import HDFileSystem
    if sys.version_info[0] == 2:
        from locket import lock_file
        with lock_file('.lock'):
            hdfs = HDFileSystem(host=host, port=port)
            bytes = hdfs.read_block(filename, offset, length, delimiter)
    else:
        hdfs = HDFileSystem(host=host, port=port)
        bytes = hdfs.read_block(filename, offset, length, delimiter)
    return bytes


def read_bytes(fn, executor=None, hdfs=None, lazy=False, delimiter=None,
               not_zero=False, **hdfs_auth):
    """ Convert location in HDFS to a list of distributed futures

    Parameters
    ----------
    fn: string
        location in HDFS
    executor: Executor (optional)
        defaults to most recently created executor
    hdfs: HDFileSystem
    not_zero: force seek of start-of-file delimiter, discarding header
    **hdfs_auth: keyword arguments
        Extra keywords to send to ``hdfs3.HDFileSystem``

    Returns
    -------
    List of ``distributed.Future`` objects
    """
    from hdfs3 import HDFileSystem
    hdfs = hdfs or HDFileSystem(**hdfs_auth)
    executor = default_executor(executor)
    blocks = get_block_locations(hdfs, fn)
    filenames = [d['filename'] for d in blocks]
    offsets = [d['offset'] for d in blocks]
    if not_zero:
        offsets = [max([o, 1]) for o in offsets]
    lengths = [d['length'] for d in blocks]
    workers = [[h.decode() for h in d['hosts']] for d in blocks]
    names = ['read-binary-%s-%d-%d' % (fn, offset, length)
            for fn, offset, length in zip(filenames, offsets, lengths)]

    logger.debug("Read %d blocks of binary bytes from %s", len(blocks), fn)
    if lazy:
        restrictions = dict(zip(names, workers))
        executor._send_to_scheduler({'op': 'update-graph',
                                     'dsk': {},
                                     'keys': [],
                                     'restrictions': restrictions,
                                     'loose_restrictions': set(names),
                                     'client': executor.id})
        values = [Value(name, [{name: (read_block_from_hdfs, fn, offset, length, hdfs.host, hdfs.port, delimiter)}])
                  for name, fn, offset, length in zip(names, filenames, offsets, lengths)]
        return values
    else:
        return executor.map(read_block_from_hdfs, filenames, offsets, lengths,
                host=hdfs.host, port=hdfs.port, delimiter=delimiter,
                workers=workers, allow_other_workers=True)


def buffer_to_csv(b, **kwargs):
    from io import BytesIO
    import pandas as pd
    bio = BytesIO(b)
    return pd.read_csv(bio, **kwargs)


@gen.coroutine
def _read_csv(path, executor=None, hdfs=None, lazy=False, lineterminator='\n',
        header=True, names=None, collection=True, **kwargs):
    from hdfs3 import HDFileSystem
    from hdfs3.core import ensure_bytes
    from dask import do
    import pandas as pd
    hdfs = hdfs or HDFileSystem()
    executor = default_executor(executor)
    kwargs['lineterminator'] = lineterminator

    filenames = walk_glob(hdfs, path)
    blockss = [read_bytes(fn, executor, hdfs, lazy=True,
                          delimiter=ensure_bytes(lineterminator))
               for fn in filenames]
    if names is None and header:
        with hdfs.open(filenames[0]) as f:
            head = pd.read_csv(f, nrows=5, **kwargs)
            names = head.columns

    dfs1 = [[do(buffer_to_csv)(blocks[0], names=names, skiprows=1, **kwargs)] +
            [do(buffer_to_csv)(b, names=names, **kwargs) for b in blocks[1:]]
            for blocks in blockss]
    dfs2 = sum(dfs1, [])
    if lazy:
        from dask.dataframe import from_imperative
        if collection:
            raise gen.Return(from_imperative(dfs2, columns=names))
        else:
            raise gen.Return(dfs2)

    else:
        futures = executor.compute(*dfs2)
        from distributed.collections import _futures_to_dask_dataframe
        if collection:
            df = yield _futures_to_dask_dataframe(futures)
            raise gen.Return(df)
        else:
            raise gen.Return(futures)


def read_csv(fn, executor=None, hdfs=None, lazy=False, **kwargs):
    """ Read CSV encoded data from bytes on HDFS

    Parameters
    ----------
    fn: string
        filename or globstring of avro files on HDFS
    lazy: boolean, optional
        If True return dask Value objects

    Returns
    -------
    List of futures of Python objects
    """
    executor = default_executor(executor)
    return sync(executor.loop, _read_csv, fn, executor, hdfs, lazy, **kwargs)


def avro_body(data, header):
    """ Convert bytes and header to Python objects

    Parameters
    ----------
    data: bytestring
        bulk avro data, without header information
    header: bytestring
        Header information collected from ``fastavro.reader(f)._header``

    Returns
    -------
    List of deserialized Python objects, probably dictionaries
    """
    import fastavro
    sync = header['sync']
    if not data.endswith(sync):
        # Read delimited should keep end-of-block delimiter
        data = data + sync
    stream = io.BytesIO(data)
    schema = header['meta']['avro.schema'].decode()
    schema = json.loads(schema)
    codec = header['meta']['avro.codec'].decode()
    return list(fastavro._reader._iter_avro(stream, header, codec,
        schema, schema))


def avro_to_df(b, av):
    """Parse avro binary data with header av into a pandas dataframe"""
    import pandas as pd
    return pd.DataFrame(data=avro_body(b, av))


@gen.coroutine
def _read_avro(path, executor=None, hdfs=None, lazy=False, **kwargs):
    """ See distributed.hdfs.read_avro for docstring """
    from hdfs3 import HDFileSystem
    from dask import do
    import fastavro
    hdfs = hdfs or HDFileSystem()
    executor = default_executor(executor)

    filenames = walk_glob(hdfs, path)

    blockss = []
    for fn in filenames:
        with hdfs.open(fn, 'r') as f:
            av = fastavro.reader(f)
            header = av._header
        schema = json.loads(header['meta']['avro.schema'].decode())

        blockss.extend([read_bytes(fn, executor, hdfs, lazy=True,
                                   delimiter=header['sync'], not_zero=True)
                       for fn in filenames])  # TODO: why is filenames used twice?

    lazy_values = [do(avro_body)(b, header) for blocks in blockss
                                            for b in blocks]

    if lazy:
        raise gen.Return(lazy_values)
    else:
        futures = executor.compute(*lazy_values)
        raise gen.Return(futures)


def read_avro(fn, executor=None, hdfs=None, lazy=False, **kwargs):
    """ Read avro encoded data from bytes on HDFS

    Parameters
    ----------
    fn: string
        filename or globstring of avro files on HDFS
    lazy: boolean, optional
        If True return dask Value objects

    Returns
    -------
    List of futures of Python objects
    """
    executor = default_executor(executor)
    return sync(executor.loop, _read_avro, fn, executor, hdfs, lazy, **kwargs)


def write_block_to_hdfs(fn, data, hdfs=None):
    """ Write bytes to HDFS """
    if not isinstance(data, bytes):
        raise TypeError("Data to write to HDFS must be of type bytes, got %s" %
                        type(data).__name__)
    with hdfs.open(fn, 'w') as f:
        f.write(data)
    return len(data)


def write_bytes(path, futures, executor=None, hdfs=None, **hdfs_auth):
    """ Write bytestring futures to HDFS

    Parameters
    ----------
    path: string
        Path on HDFS to write data.  Either globstring like ``/data/file.*.dat``
        or a directory name like ``/data`` (directory will be created)
    futures: list
        List of futures.  Each future should refer to a block of bytes.
    executor: Executor
    hdfs: HDFileSystem

    Returns
    -------
    Futures that wait until writing is complete.  Returns the number of bytes
    written.

    Examples
    --------

    >>> write_bytes('/data/file.*.dat', futures, hdfs=hdfs)  # doctest: +SKIP
    >>> write_bytes('/data/', futures, hdfs=hdfs)  # doctest: +SKIP
    """
    from hdfs3 import HDFileSystem
    hdfs = hdfs or HDFileSystem(**hdfs_auth)
    executor = default_executor(executor)

    n = len(futures)
    n_digits = int(log(n) / log(10))
    template = '%0' + str(n_digits) + 'd'

    if '*' in path:
        dirname = os.path.split(path)[0]
        hdfs.mkdir(dirname)
        filenames = [path.replace('*', template % i) for i in range(n)]
    else:
        hdfs.mkdir(path)
        filenames = [os.path.join(path, template % i) for i in range(n)]

    return executor.map(write_block_to_hdfs, filenames, futures, hdfs=hdfs)
