import zmq
import trio

# I don't know how to make MessageTracker work
# Poller and select() I think we can just skip
# zmq.device is deprecated, so we can skip it
# Not sure about all the stuff in zmq.devices

# zmq.auth.ioloop looks pretty simple to adapt to have a TrioAuthenticator

# Context and Socket need non-trivial wrappers of course
# I wish ZMQ_FD was properly documented

# so I think the way ZMQ_FD works is:

# internally, each socket keeps readable/writable state, and also has a
# "command queue". they never update readable/writable state directly; instead
# commands are sent through the queue, and when the queue is processed, the
# state gets updated.
#
# ZMQ_FD is the self-pipe used to signal that there are commands ready to
# process
#
# getsockopt(ZMQ_EVENTS), send(), and recv() all process the command queue
# -- so after calling one of them, ZMQ_FD will be non-readable, and ZMQ_EVENTS
# will be up to date. (see socket_base_t::process_commands and its callers)
#
# Any change in ZMQ_EVENTS is necessarily preceded by the fd becoming
# readable, and then one of these calls.
#
# So, it's totally valid to try doing a non-blocking send/recv, and if that
# fails with ZMQ_AGAIN then watch the fd until it becomes readable *or* some
# other getsockopt/send/recv call is made, because there could be a race
# condition where when one of those is called it might immediately clear the
# ZMQ_FD before our poller notices that it's readable.


# https://lists.zeromq.org/pipermail/zeromq-dev/2015-July/028738.html
# https://github.com/aio-libs/aiozmq/blob/13d8f4ec564f072b334d52df615237da1b7b76cd/aiozmq/core.py#L530-L541

# This is... kind of a problem for duplex operation, isn't it? because if you
# try sending and go to sleep, and then you try recving and go to sleep...
# then the recv might have swallowed the notification for send, so you have to
# wake up the send and try again; and then the send might have swallowed the
# notification for recv, so you have to wake up the recv and try again; and
# then ...

# I guess the way out is that you always check ZMQ_EVENTS after every
# send/recv call, and after ZMQ_FD becomes readable. And you never call
# send/recv unless ZMQ_EVENTS says to.

class Socket:
    def __init__(self, zmq_sock):
        self._events = None
        self._waiting = set()
        self._zmq_sock = zmq_sock
        self._trigger = trio.socket.fromfd(zmq_sock.fd)
        self._trigger_lock = trio.Lock()

        self._send_guard = trio._util.ConflictDetector()
        self._recv_guard = trio._util.ConflictDetector()

        self._update_events()



    # This *must* be called after every call to send, recv,
    # getsockopt(zmq.EVENTS), or anything else that might process the internal
    # command queue.
    def _update_events(self):
        self._events = self._zmq_sock.getsockopt(zmq.EVENTS)
        while self._waiting:
            trio.hazmat.reschedule(self._waiting.pop())

    async def _wait_for_update(self):
        task = trio.hazmat.current_task()
        self._waiting.add(task)
        def abort(_):
            self._waiting.remove(task)
            return trio.hazmat.Abort.SUCCEEDED
        await trio.hazmat.yield_indefinitely(abort)

    @trio.hazmat.ki_protection_enabled
    async def _wait_for(self, flag):
        while not self._events & flag:
            try:
                self._trigger_lock.acquire_nowait()
            except trio.WouldBlock:
                # Someone else is on the job
                await self._wait_for_update()
            else:
                try:
                    await trio.hazmat.wait_socket_readable(self._trigger)
                finally:
                    self._update_events()
                    self._trigger_lock.release()

    # So the problem with the above is, if one task is sitting blocked in
    # wait_socket_readable, and another task comes along and successfully does
    # a send(), then the events might get updated and we need to wake up the
    # task blocked in wait_socket_readable.

    # maybe we just block in either sleep_forever or wait_socket_readable, and
    # when _update_events always calls cancel() on everyone's sleeping tasks.

class Socket:
    def __init__(self, zmq_sock):
        self._zmq_sock = zmq_sock

        # The current ZMQ_EVENTS
        self._zmq_events = None
        # Cancel scopes for everyone who's waiting for _zmq_events to change
        self._cancel_scopes = set()

        # the ZMQ_FD fd
        # fromfd dups it, which is what we want, b/c the Python socket object
        # will close the fd when it's destroyed. This may or may not actually
        # be a socket, but it's definitely selectable. So on Windows it has to
        # be a socket handle, and on Unix it's some sort of file descriptor,
        # and in both cases that's what the socket module expects. And all
        # we're going to do is to call wait_readable() and close() on it,
        # which are operations that are defined for all fds on Unix.
        self._trigger = socket.fromfd(zmq_sock.fd)
        self._someone_is_watching_trigger = False

        self._something_happened()

    def close(self):
        self._trigger.close()
        self._zmq_sock.close()

    def _something_happened(self):
        self._events = self._zmq_sock.getsockopt(zmq.EVENTS)
        for cancel_scope in self._cancel_scopes:
            cancel_scope.cancel()

    @contextlib.contextmanager
    def _cancel_when_something_happens(self):
        with trio.open_cancel_scope() as cancel_scope:
            try:
                self._cancel_scopes.add(cancel_scope)
                yield
            finally:
                self._cancel_scopes.remove(cancel_scope)

    @trio.hazmat.enable_ki_protection
    async def _wait_for_something_to_happen(self):
        with self._cancel_when_something_happens():
            if not self._someone_is_watching_trigger:
                self._someone_is_watching_trigger = True
                try:
                    await trio.hazmat.wait_socket_readable(self._trigger)
                finally:
                    self._someone_is_watching_trigger = False
                    self._something_happened()
            else:
                # Someone else is watching the socket, so we just need to wait
                # for _something_happened to wake us up.
                await trio.sleep_forever()

    # XX this is checkpoint correct but not sure if we want it to be
    async def _wait_for_flag(self, flag):
        if self._events & flag:
            await trio.hazmat.yield_briefly()
        else:
            while not (self._events & flag):
                await self._wait_for_something_to_happen()

    # XX needs conflict detection
    # ...or does it? each send() call is atomic, like a DGRAM socket.
    async def send(self, data, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for_flag(zmq.POLLOUT)
            try:
                return self._zmq_sock.send(data, flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._something_happened()

    # XX needs conflict detection
    async def recv(self, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for_flag(zmq.POLLIN)
            try:
                return self._zmq_sock.recv(flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._something_happened()

# XX I think the algorithm above is correct, modulo conflict detection. But
# it's inefficient in some cases. Imagine you have a duplex socket like a
# ROUTER or DEALER, and one task is constantly sending while another is
# sitting blocked in recv. In the above, the task in recv keeps waking up
# after each send, discovers that yeah, still nothing to recv, and goes back
# to sleep.
#
# This could be made more efficient using the following observation: so long
# as there is a task blocked in wait_socket_readable, and _something_happened
# does not change the events -- or even better, does not signal an event that
# anyone is waiting for -- then we don't need to wake anyone up.

# Another way to think about it:
# - if the socket becomes readable, anyone waiting for read needs to be woken
#   up
# - if the socket becomes writable, anyone waiting for write needs to be woken
#   up
# - if the task blocked in wait_readable wakes up, then it needs to wake up at
#   least one sleeping task. waking everyone is maybe easier to be certain of
#   correctness and in practice probably identical b/c who has >2 tasks
#   talking on the same socket?

# Hence:

class Socket:
    def __init__(self, zmq_sock):
        self._zmq_sock = zmq_sock

        # The current ZMQ_EVENTS
        self._zmq_events = None
        # Cancel scopes for everyone who's waiting for _zmq_events to change
        self._cancel_scopes = {zmq.POLLIN: set(), zmq.POLLOUT: set()}

        # the ZMQ_FD fd
        # fromfd dups it, which is what we want, b/c the Python socket object
        # will close the fd when it's destroyed. This may or may not actually
        # be a socket, but it's definitely selectable. So on Windows it has to
        # be a socket handle, and on Unix it's some sort of file descriptor,
        # and in both cases that's what the socket module expects. And all
        # we're going to do is to call wait_readable() and close() on it,
        # which are operations that are defined for all fds on Unix.
        self._trigger = socket.fromfd(zmq_sock.fd)
        self._someone_is_watching_trigger = False

        self._update_events()

    def close(self):
        self._trigger.close()
        # XX zmq.sugar.Socket has some subtlety around shadow sockets
        self._zmq_sock.close()

    def _wake(self, flag):
        for cancel_scope in self._cancel_scopes[flag]:
            cancel_scope.cancel()

    def _update_events(self, wake_all=False):
        self._events = self._zmq_sock.getsockopt(zmq.EVENTS)
        if wake_all or self._events & zmq.POLLIN:
            self._wake(zmq.POLLIN)
        if wake_all or self._events & zmq.POLLOUT:
            self._wake(zmq.POLLOUT)

    @contextlib.contextmanager
    def _cancel_when(self, flag):
        with trio.open_cancel_scope() as cancel_scope:
            try:
                self._cancel_scopes[flag].add(cancel_scope)
                yield
            finally:
                self._cancel_scopes[flag].remove(cancel_scope)

    @trio.hazmat.enable_ki_protection
    async def _wait_for(self, flag):
        if self._events & flag:
            await trio.hazmat.yield_briefly()
            return

        while not self._events & flag:
            with self._cancel_when(flag):
                if not self._someone_is_watching_trigger:
                    self._someone_is_watching_trigger = True
                    try:
                        await trio.hazmat.wait_socket_readable(self._trigger)
                    finally:
                        self._someone_is_watching_trigger = False
                        # Wake up everyone, regardless of whether their event
                        # is ready, b/c they need to potentially take over the
                        # job of sitting in wait_socket_readable.
                        self._update_events(wake_all=True)
                else:
                    # Someone else is watching the socket, so we just need to
                    # wait to be woken up.
                    await trio.sleep_forever()

    # XX needs conflict detection
    # ...or does it? each send() call is atomic, like a DGRAM socket.
    async def send(self, data, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for(zmq.POLLOUT)
            try:
                return self._zmq_sock.send(data, flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._update_events()

    # XX needs conflict detection, maybe?
    async def recv(self, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for(zmq.POLLIN)
            try:
                return self._zmq_sock.recv(flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._update_events()


class Socket:
    def __init__(self, zmq_sock):
        self._zmq_sock = zmq_sock

        # The current ZMQ_EVENTS
        self._zmq_events = None
        # Cancel scopes for everyone who's waiting for _zmq_events to change
        self._cancel_scopes = {zmq.POLLIN: set(), zmq.POLLOUT: set()}

        # the ZMQ_FD fd
        # fromfd dups it, which is what we want, b/c the Python socket object
        # will close the fd when it's destroyed. This may or may not actually
        # be a socket, but it's definitely selectable. So on Windows it has to
        # be a socket handle, and on Unix it's some sort of file descriptor,
        # and in both cases that's what the socket module expects. And all
        # we're going to do is to call wait_readable() and close() on it,
        # which are operations that are defined for all fds on Unix.
        self._trigger = socket.fromfd(zmq_sock.fd)
        self._someone_is_watching_trigger = False

        self._update_events()

    def close(self):
        self._trigger.close()
        # XX zmq.sugar.Socket has some subtlety around shadow sockets
        self._zmq_sock.close()

    def _wake(self, flag):
        for cancel_scope in self._cancel_scopes[flag]:
            cancel_scope.cancel()

    def _update_events(self, wake_all=False):
        self._events = self._zmq_sock.getsockopt(zmq.EVENTS)
        if wake_all or self._events & zmq.POLLIN:
            self._wake(zmq.POLLIN)
        if wake_all or self._events & zmq.POLLOUT:
            self._wake(zmq.POLLOUT)

    @contextlib.contextmanager
    def _cancel_when(self, flag):
        with trio.open_cancel_scope() as cancel_scope:
            try:
                self._cancel_scopes[flag].add(cancel_scope)
                yield
            finally:
                self._cancel_scopes[flag].remove(cancel_scope)

    @trio.hazmat.enable_ki_protection
    async def _wait_for(self, flag):
        if self._events & flag:
            await trio.hazmat.yield_briefly()
            return

        while not self._events & flag:
            with self._cancel_when(flag):
                if not self._someone_is_watching_trigger:
                    self._someone_is_watching_trigger = True
                    try:
                        await trio.hazmat.wait_socket_readable(self._trigger)
                    finally:
                        self._someone_is_watching_trigger = False
                        # Wake up everyone, regardless of whether their event
                        # is ready, b/c they need to potentially take over the
                        # job of sitting in wait_socket_readable.
                        self._update_events(wake_all=True)
                else:
                    # Someone else is watching the socket, so we just need to
                    # wait to be woken up.
                    await trio.sleep_forever()

    # XX needs conflict detection
    # ...or does it? each send() call is atomic, like a DGRAM socket.
    async def send(self, data, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for(zmq.POLLOUT)
            try:
                return self._zmq_sock.send(data, flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._update_events()

    # XX needs conflict detection, maybe?
    async def recv(self, flags=0, copy=True, track=False):
        flags |= zmq.NOBLOCK
        while True:
            await self._wait_for(zmq.POLLIN)
            try:
                return self._zmq_sock.recv(flags, copy, track)
            except zmq.Again:
                pass
            finally:
                self._update_events()


    # XX we need to also _update_events whenever the *user* calls
    # getsockopt(EVENTS)
    #
    # this is actually pretty obnoxious because there are so many paths that
    # do this. (foo.events, foo.get(EVENTS), foo.getsockopt_string("EVENTS"),
    # etc. etc.)
    # ...it *looks* like they all funnel down to .get or .getsockopt?

    # XX should probably implement poll() because why not, it's basically
    # _wait_for except it can handle waiting for either-read-or-write, which
    # is trivial to support

    # get_monitor_socket might need to be overridden, b/c it calls connect()
    # on a new context socket

    # ughh do I *really* need to subclass zmq.Socket?

    # set(SUBSCRIBE, ...) or set(UNSUBSCRIBE, ...) can also trigger a change
    # in event availability, apparently. Or at least zmq/green/core.py thinks
    # so.
    # Maybe this caused crashes or something? weird.
    # http://zeromq-dev.zeromq.narkive.com/lasr2cwh/xpub-or-pub-aborted-by-assertion-failure-erased-1-on-mtrie-cpp
    # Ah, the claim is that these trigger a send() internally, awesome:
    # https://github.com/zeromq/pyzmq/pull/951
    #
    # what else does this?

    # src/socket_base.cpp, looking for calls to process_commands
    #
    # getsockopt only for ZMQ_EVENTS
    #   called only by the polling logic, which we don't use
    # bind()
    # connect()
    # term_endpoint()
    # send()
    # recv()
    # in_event() -- "invoked only once the socket is running in the context of
    # the reaper thread"

    # kinda hard to tell who calls these

    # src/sub.cpp calls xsub_t::xsend(&msg) when it sees a (UN)SUBSCRIBE, not
    # sure how that resolves exactly but I believe that's some kind of send()

    # probably we should just check after every operation, we already have to
    # check after most of them (including the most performance-sensitive ones)
    # and it's easier than faffing about guessing

    # except close, close should wake everyone up no matter what
