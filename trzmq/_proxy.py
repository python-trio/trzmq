# The real zmq.proxy calls a C function zmq_proxy, which does an infinite
# synchronous loop. Not very friendly -- even if we run it in a thread, we
# have no way to stop it, except to close the whole context.
#
# https://github.com/zeromq/libzmq/blob/master/doc/zmq_proxy.txt
# https://github.com/zeromq/libzmq/blob/master/src/proxy.cpp
#
# Differences:
#
# - I haven't bothered to implement the control socket functionality exposed
#   by zmq_proxy_steerable. It would be pretty easy to do, but you don't need
#   it to quit out of this. (I guess you might need it if you want to quit
#   without dropping any messages.)
#
# - This implementation reads a message from the source, then waits until the
#   capture and sink are writable before sending it to them. The C++
#   implementation waits until the sink (at least?) is writable before even
#   reading from the source. So we have *slightly* higher buffering, by like
#   0.5 messages on average or something like that.

import trio

__all__ = ["proxy"]

async def _proxy_one_way(source, sink, capture):
    while True:
        message = await source.recv_multipart()
        if capture is not None:
            await capture.send_multipart(capture)
        await sink.send_multipart(message)

async def proxy(frontend, backend, capture=None):
    async with trio.open_nursery() as nursery:
        nursery.start_soon(_proxy_one_way, frontend, backend, capture)
        nursery.start_soon(_proxy_one_way, backend, frontend, capture)
