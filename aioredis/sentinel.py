import asyncio
import random

from .connection import create_connection
from .errors import MasterNotFoundError, SlaveNotFoundError, RedisError, \
    ReadOnlyError
from .commands import create_redis, Redis


class SentinelManagedConnection(object):
    closed = False

    def __init__(self, sentinel_service, **conn_kwargs):
        self._sentinel_service = sentinel_service
        self._conn_kwargs = conn_kwargs
        self._conn = None
        self._loop = conn_kwargs.get('loop')
        self._lock = asyncio.Lock(loop=self._loop)


    def close(self):
        self._conn.close()

    @asyncio.coroutine
    def wait_closed(self):
        yield from self._conn.wait_closed()

    @asyncio.coroutine
    def execute(self, *args, **kwargs):
        first_time = True

        while True:
            conn = yield from self.get_atomic_connection()
            try:
                return (yield from conn.execute(*args, **kwargs))
            except ReadOnlyError:
                if self._sentinel_service.is_master:
                    # When talking to a master, a ReadOnlyError when likely
                    # indicates that the previous master that we're still connected
                    # to has been demoted to a slave and there's a new master.
                    # calling disconnect will force the connection to re-query
                    # sentinel during the next connect() attempt.
                    self._conn.close()
                    yield from self._conn.wait_closed()
                    if first_time:
                        first_time = False
                        continue
                    raise ConnectionError('The previous master is now a slave')
                raise

    @asyncio.coroutine
    def execute_pubsub(self, *args, **kwargs):
        first_time = True

        while True:
            conn = yield from self.get_atomic_connection()
            try:
                return (yield from conn.execute_pubsub(*args, **kwargs))
            except ReadOnlyError:
                if self._sentinel_service.is_master:
                    # When talking to a master, a ReadOnlyError when likely
                    # indicates that the previous master that we're still connected
                    # to has been demoted to a slave and there's a new master.
                    # calling disconnect will force the connection to re-query
                    # sentinel during the next connect() attempt.
                    self._conn.close()
                    yield from self._conn.wait_closed()
                    if first_time:
                        first_time = False
                        continue
                    raise ConnectionError('The previous master is now a slave')
                raise

    @asyncio.coroutine
    def get_atomic_connection(self):
        if self._conn is None or self._conn.closed:
            with (yield from self._lock):
                if self._conn is None or self._conn.closed:
                    if self._sentinel_service.is_master:
                        addr = yield from self._sentinel_service.get_master_address()
                        conn = yield from create_connection(
                            addr, **self._conn_kwargs)
                        self._conn = conn
                    else:
                        slave = yield from self.connection_pool.rotate_slaves()
                        while slave is not None:
                            try:
                                conn = yield from create_connection(
                                    slave, **self._conn_kwargs)
                                self._conn = conn
                            except SlaveNotFoundError:
                                raise
                            except RedisError:
                                slave = yield from self.connection_pool.rotate_slaves()

                        raise SlaveNotFoundError
        return self._conn


@asyncio.coroutine
def create_sentinel_connection(sentinel_service, loop=None,
                               **connection_kwargs):
    conn = SentinelManagedConnection(sentinel_service, loop=loop,
                                     **connection_kwargs)
    return Redis(conn)


@asyncio.coroutine
def create_sentinel(sentinels, *, db=None, password=None,
                    encoding=None, min_other_sentinels=0, loop=None):
    """Creates redis sentinel

    `sentinels`` is a list of sentinel nodes. Each node is represented by
    a pair (hostname, port).

    ``min_other_sentinels`` defined a minimum number of peers for a sentinel.
    When querying a sentinel, if it doesn't meet this threshold, responses
    from that sentinel won't be considered valid.

    Return value is RedisSentinel instance.

    """

    sentinels_connections = []
    for hostname, port in sentinels:
        sentinel = yield from create_redis((hostname, port), db=db,
                                           password=password,
                                           encoding=encoding, loop=loop)
        sentinels_connections.append(sentinel)
    return RedisSentinel(sentinels_connections, min_other_sentinels, loop=loop)


class RedisSentinelService:
    def __init__(self, service_name, sentinel, is_master):
        self.is_master = is_master
        self.service_name = service_name
        self.sentinel = sentinel
        self.master_address = None
        self.slave_rr_counter = None

    @asyncio.coroutine
    def get_master_address(self):
        master_address = yield from self.sentinel.discover_master(
            self.service_name)
        if self.is_master:
            if self.master_address is None:
                self.master_address = master_address
            elif master_address != self.master_address:
                # Master address changed, disconnect all clients in this pool
                # TODO
                pass
        return master_address

    @asyncio.coroutine
    def rotate_slaves(self):
        "Round-robin slave balancer"
        slaves = yield from self.sentinel.discover_slaves(self.service_name)
        if slaves:
            if self.slave_rr_counter is None:
                self.slave_rr_counter = 0
            if self.slave_rr_counter < len(slaves):
                slave = slaves[self.slave_rr_counter]
                self.slave_rr_counter += 1
                return slave
        # Fallback to the master connection
        try:
            if self.slave_rr_counter == len(slaves):
                master = yield from self.get_master_address()
                return master
            self.slave_rr_counter = 0
        except MasterNotFoundError:
            pass
        raise SlaveNotFoundError('No slave found for %r' % (self.service_name))


class RedisSentinel:
    """
    Redis Sentinel cluster client
    """
    def __init__(self, sentinels, min_other_sentinels=0, loop=None):
        self.sentinels = sentinels
        self.min_other_sentinels = min_other_sentinels
        self.loop = loop
        self.services = {}

    def close(self):
        for sentinel in self.sentinels:
            sentinel.close()

    @asyncio.coroutine
    def wait_closed(self):
        for sentinel in self.sentinels:
            yield from sentinel.wait_closed()

    def check_master_state(self, state, service_name):
        if not state['is_master'] or state['is_sdown'] or state['is_odown']:
            return False
        # Check if our sentinel doesn't see other nodes
        if state['num-other-sentinels'] < self.min_other_sentinels:
            return False
        return True

    @asyncio.coroutine
    def discover_master(self, service_name):
        """
        Asks sentinel servers for the Redis master's address corresponding
        to the service labeled ``service_name``.
        Returns a pair (address, port) or raises MasterNotFoundError if no
        master is found.
        """
        for sentinel_no, sentinel in enumerate(self.sentinels):
            try:
                masters = yield from sentinel.sentinel_masters()
            except RedisError:
                continue
            state = masters.get(service_name)
            if state and self.check_master_state(state, service_name):
                # Put this sentinel at the top of the list
                self.sentinels[0], self.sentinels[sentinel_no] = (
                    sentinel, self.sentinels[0])
                return state['ip'], state['port']
        raise MasterNotFoundError("No master found for %r" % (service_name,))

    def filter_slaves(self, slaves):
        "Remove slaves that are in an ODOWN or SDOWN state"
        slaves_alive = []
        for slave in slaves:
            if slave['is_odown'] or slave['is_sdown']:
                continue
            slaves_alive.append((slave['ip'], slave['port']))
        return slaves_alive

    @asyncio.coroutine
    def discover_slaves(self, service_name):
        "Returns a list of alive slaves for service ``service_name``"
        for sentinel in self.sentinels:
            try:
                slaves = yield from sentinel.sentinel_slaves(service_name)
            except RedisError:
                continue
            slaves = self.filter_slaves(slaves)
            if slaves:
                return slaves
        return []

    @asyncio.coroutine
    def master_for(self, service_name, **kwargs):
        """
        Returns a redis client instance for the ``service_name`` master.
        A SentinelConnectionPool class is used to retrive the master's
        address before establishing a new connection.

        NOTE: If the master's address has changed, any cached connections to
        the old master are closed.

        All other keyword arguments are merged with any connection_kwargs
        passed to this class and passed to the connection pool as keyword
        arguments to be used to initialize Redis connections.
        """
        connection_kwargs = dict()
        connection_kwargs.update(kwargs)
        if service_name not in self.services:
            self.services[service_name] = {}
        if 'master' not in self.services[service_name]:
            self.services[service_name]['master'] = \
                RedisSentinelService(service_name, self, True)
        service = self.services[service_name]['master']
        conn = yield from create_sentinel_connection(service, **connection_kwargs)
        return conn

    @asyncio.coroutine
    def slave_for(self, service_name, **kwargs):
        """
        Returns redis client instance for the ``service_name`` slave(s).

        A SentinelConnectionPool class is used to retrive the slave's
        address before establishing a new connection.

        All other keyword arguments are merged with any connection_kwargs
        passed to this class and passed to the connection pool as keyword
        arguments to be used to initialize Redis connections.
        """
        connection_kwargs = dict()
        connection_kwargs.update(kwargs)
        if service_name not in self.services:
            self.services[service_name] = {}
        if 'slave' not in self.services[service_name]:
            self.services[service_name]['slave'] = \
                RedisSentinelService(service_name, self, True)
        service = self.services[service_name]['slave']
        conn = yield from create_sentinel_connection(service, **connection_kwargs)
        return conn