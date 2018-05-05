import zmq

import trio

__all__ = []

def reexport(name):
    __all__.append(name)
    globals()[name] = getattr(zmq, name)

for name, value in vars(zmq).items():
    if name.isupper():
        reexport(name)
    if isinstance(value, type) and issubclass(value, BaseException):
        reexport(name)

for name in ["zmq_version", "pyzmq_version", "zmq_version_info",
             "pyzmq_version_info", "has"]:
    reexport(name)

# This is CPU-intensive but can/should drop the GIL, so running in a thread
# makes sense. (XX as of 2017-08-24 I don't think pyzmq actually does drop the
# GIL here when using the cython backend, this would be an easy patch.)
async def curve_keypair():
    return trio.run_sync_in_worker_thread(zmq.curve_keypair, cancellable=True)

__all__.append("curve_keypair")
