"""Context."""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from collections import defaultdict
import itertools
import logging
import pickle
import time

from . import __version__ as PYSPARKLING_VERSION
from .broadcast import Broadcast
from .cache_manager import CacheManager
from .fileio import File, TextFile
from .partition import Partition
from .rdd import RDD
from .task_context import TaskContext

log = logging.getLogger(__name__)


def unit_fn(arg):
    """Used as dummy serializer and deserializer."""
    return arg


def runJob_map(i):
    t_start = time.clock()
    (deserializer, data_serializer, data_deserializer,
     serialized, serialized_data, cache_manager) = i

    if cache_manager:
        if not CacheManager.singleton__:
            CacheManager.singleton__ = data_deserializer(cache_manager)
        else:
            CacheManager.singleton().join(
                data_deserializer(cache_manager).cache_obj
            )
    cm_state = CacheManager.singleton().stored_idents()
    t_cache_init = time.clock()

    func, rdd = deserializer(serialized)
    t_deserialize_func = time.clock()
    partition = data_deserializer(serialized_data)
    t_deserialize_data = time.clock()

    task_context = TaskContext(stage_id=0, partition_id=partition.index)
    result = func(task_context, rdd.compute(partition, task_context))
    t_exec = time.clock()

    return data_serializer((
        result,
        CacheManager.singleton().get_not_in(cm_state),
        {
            'map_cache_init': t_cache_init - t_start,
            'map_deserialize_func': t_deserialize_func - t_cache_init,
            'map_deserialize_data': t_deserialize_data - t_deserialize_func,
            'map_exec': t_exec - t_deserialize_data,
        }
    ))


class Context(object):
    """Context object similar to a Spark Context.

    The variable `_stats` contains measured timing information about data and
    function (de)serialization and workload execution to benchmark your jobs.

    :param pool:
        An instance with a ``map(func, iterable)`` method.

    :param serializer:
        Serializer for functions. Examples are ``pickle.dumps`` and
        ``dill.dumps``.

    :param deserializer:
        Deserializer for functions. Examples are ``pickle.loads`` and
        ``dill.loads``.

    :param data_serializer:
        Serializer for the data.

    :param data_deserializer:
        Deserializer for the data.

    """

    __last_rdd_id = 0

    def __init__(self, pool=None, serializer=None, deserializer=None,
                 data_serializer=None, data_deserializer=None,
                 textfile_delimiter=None):
        if not pool:
            pool = DummyPool()
        if not serializer:
            serializer = unit_fn
        if not deserializer:
            deserializer = unit_fn
        if not data_serializer:
            data_serializer = unit_fn
        if not data_deserializer:
            data_deserializer = unit_fn

        self._pool = pool
        self._serializer = serializer
        self._deserializer = deserializer
        self._data_serializer = data_serializer
        self._data_deserializer = data_deserializer
        self._textfile_delimiter = textfile_delimiter
        self._s3_conn = None
        self._stats = defaultdict(float)

        self.version = PYSPARKLING_VERSION

    def broadcast(self, x):
        return Broadcast(x)

    def newRddId(self):
        Context.__last_rdd_id += 1
        return Context.__last_rdd_id

    def parallelize(self, x, numPartitions=None):
        """parallelize x

        :param x:
            An iterable (e.g. a list) that represents the data.

        :param numPartitions: (optional)
            The number of partitions the data should be split into.
            A partition is a unit of data that is processed at a time.
            Can be ``None``.

        :returns:
            New RDD.

        """
        if not numPartitions:
            return RDD([Partition(x, 0)], self)

        x_len_iter, x = itertools.tee(x, 2)
        len_x = sum(1 for _ in x_len_iter)

        def partitioned():
            for i in range(numPartitions):
                start = int(i * len_x / numPartitions)
                end = int((i + 1) * len_x / numPartitions)
                if i + 1 == numPartitions:
                    end += 1
                yield itertools.islice(x, end - start)

        return self._parallelize_partitions(partitioned())

    def _parallelize_partitions(self, partitions):
        """helper to parallelize partitions

        :param partitions:
            An iterable over the partitioned data.

        :returns:
            New RDD.

        """

        return RDD(
            (Partition(p_data, i) for i, p_data in enumerate(partitions)),
            self,
        )

    def pickleFile(self, name, minPartitions=None):
        """read a pickle file

        Reads files created with :func:`RDD.saveAsPickleFile()` into an RDD.

        :param name:
            Location of a file. Can include schemes like ``http://``,
            ``s3://`` and ``file://``, wildcard characters ``?`` and ``*``
            and multiple expressions separated by ``,``.

        :param minPartitions: (optional)
            By default, every file is a partition, but this option allows to
            split these further.

        :returns:
            New RDD.


        Example with a serialized list:

        >>> import pickle
        >>> from pysparkling import Context
        >>> from tempfile import NamedTemporaryFile
        >>> tmpFile = NamedTemporaryFile(delete=True)
        >>> tmpFile.close()
        >>> with open(tmpFile.name, 'wb') as f:
        ...     pickle.dump(['hello', 'world'], f)
        >>> Context().pickleFile(tmpFile.name).collect()[0] == 'hello'
        True

        """
        resolved_names = File.resolve_filenames(name)
        log.debug('pickleFile() resolved "{0}" to {1} files.'
                  ''.format(name, len(resolved_names)))

        num_partitions = len(resolved_names)
        if minPartitions and minPartitions > num_partitions:
            num_partitions = minPartitions

        rdd_filenames = self.parallelize(resolved_names, num_partitions)
        rdd = rdd_filenames.flatMap(
            lambda f_name: pickle.load(File(f_name).load())
        )
        rdd._name = name
        return rdd

    def runJob(self, rdd, func, partitions=None, allowLocal=False,
               resultHandler=None):
        """This function is used by methods in the RDD.

        Note that the maps are only inside generators and the resultHandler
        needs to take care of executing the ones that it needs. In other words,
        if you need everything to be executed, the resultHandler needs to be
        at least ``lambda x: list(x)`` to trigger execution of the generators.

        :param func:
            Map function. The signature is
            func(TaskContext, Iterator over elements).

        :param partitions: (optional)
            List of partitions that are involved. Default is ``None``, meaning
            the map job is applied to all partitions.

        :param allowLocal: (optional)
            Allows for local execution. Default is False.

        :param resultHandler: (optional)
            Process the result from the maps.

        :returns:
            Result of resultHandler.

        """

        if not partitions:
            partitions = rdd.partitions()

        # this is the place to insert proper schedulers
        if allowLocal or isinstance(self._pool, DummyPool):
            map_result = self._runJob_local(rdd, func, partitions)
        else:
            map_result = self._runJob_distributed(rdd, func, partitions)
        log.debug('Map jobs generated.')

        if resultHandler is not None:
            return resultHandler(map_result)
        return list(map_result)  # convert to list to execute on all partitions

    def _runJob_local(self, rdd, func, partitions):
        for partition in partitions:
            task_context = TaskContext(
                stage_id=0,
                partition_id=partition.index,
            )
            yield func(task_context, rdd.compute(partition, task_context))

    def _runJob_distributed(self, rdd, func, partitions):
        cm = CacheManager.singleton()
        serialized_func_rdd = self._serializer((func, rdd))

        def prepare(p):
            t_start = time.clock()
            cm_clone = cm.clone_contains(lambda i: i[1] == p.index)
            self._stats['driver_cache_clone'] += (time.clock() -
                                                  t_start)

            t_start = time.clock()
            cm_serialized = self._data_deserializer(cm_clone)
            self._stats['driver_cache_serialize'] += (time.clock() -
                                                      t_start)

            t_start = time.clock()
            serialized_p = self._data_deserializer(p)
            self._stats['driver_serialize_data'] += (time.clock() -
                                                     t_start)

            return (
                self._deserializer,
                self._data_serializer,
                self._data_deserializer,
                serialized_func_rdd,
                serialized_p,
                cm_serialized,
            )

        for d in self._pool.map(runJob_map,
                                (prepare(p) for p in partitions)):
            t_start = time.clock()
            map_result, cache_result, s = self._data_deserializer(d)
            self._stats['driver_deserialize_data'] += (time.clock() -
                                                       t_start)

            # join cache
            t_start = time.clock()
            cm.join(cache_result)
            self._stats['driver_cache_join'] += time.clock() - t_start

            # collect stats
            for k, v in s.items():
                self._stats[k] += v

            yield map_result

    def textFile(self, filename, minPartitions=None, use_unicode=True):
        """Read a text file into an RDD.

        :param filename:
            Location of a file. Can include schemes like ``http://``,
            ``s3://`` and ``file://``, wildcard characters ``?`` and ``*``
            and multiple expressions separated by ``,``.

        :param minPartitions: (optional)
            By default, every file is a partition, but this option allows to
            split these further.

        :param use_unicode: (optional, default=True)
            Use ``utf8`` if ``True`` and ``ascii`` if ``False``.

        :returns:
            New RDD.

        """
        resolved_names = TextFile.resolve_filenames(filename)
        log.debug('textFile() resolved "{0}" to {1} files.'
                  ''.format(filename, len(resolved_names)))

        num_partitions = len(resolved_names)
        if minPartitions and minPartitions > num_partitions:
            num_partitions = minPartitions

        encoding = 'utf8' if use_unicode else 'ascii'

        rdd_filenames = self.parallelize(resolved_names, num_partitions)
        if self._textfile_delimiter:
            rdd = rdd_filenames.flatMap(
                lambda f_name:
                TextFile(f_name).load(encoding=encoding).read().split(self._textfile_delimiter)
            )
        else:
            rdd = rdd_filenames.flatMap(
                lambda f_name:
                TextFile(f_name).load(encoding=encoding).read().splitlines()
            )
        rdd._name = filename
        return rdd

    def union(self, rdds):
        """union

        :param rdds:
            Iterable of RDDs.

        :returns:
            New RDD.

        """
        return self.parallelize(
            (x for rdd in rdds for x in rdd.collect())
        )

    def wholeTextFiles(self, path, minPartitions=None, use_unicode=True):
        """Read text files into an RDD of pairs of file name and file content.

        :param path:
            Location of the files. Can include schemes like ``http://``,
            ``s3://`` and ``file://``, wildcard characters ``?`` and ``*``
            and multiple expressions separated by ``,``.

        :param minPartitions: (optional)
            By default, every file is a partition, but this option allows to
            split these further.

        :param use_unicode: (optional, default=True)
            Use ``utf8`` if ``True`` and ``ascii`` if ``False``.

        :returns:
            New RDD.

        """
        resolved_names = TextFile.resolve_filenames(path)
        log.debug('wholeTextFiles() resolved "{0}" to {1} files.'
                  ''.format(path, len(resolved_names)))

        num_partitions = len(resolved_names)
        if minPartitions and minPartitions > num_partitions:
            num_partitions = minPartitions

        encoding = 'utf8' if use_unicode else 'ascii'
        rdd_filenames = self.parallelize(
            [(f_name, encoding) for f_name in resolved_names],
            num_partitions,
        )
        rdd = rdd_filenames.map(map_whole_text_file)
        rdd._name = path
        return rdd


class DummyPool(object):
    def __init__(self):
        pass

    def map(self, f, input_list):
        return (f(x) for x in input_list)

    def close(self):
        pass

    def shutdown(self):
        pass


# pickle-able helpers

def map_whole_text_file(f_name__encoding):
    f_name, encoding = f_name__encoding
    return (
        f_name,
        TextFile(f_name).load(encoding=encoding).read(),
    )
