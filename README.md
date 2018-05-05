# This is not real yet

This is some unedited notes I made towards getting ZeroMQ working in
Trio. The general idea is to expose a similar API to pyzmq, but with
async in the appropriate places. I'm putting it up because a lot of
people seem to be interested in getting this working so I'm hoping
they take it and run with it :-).

The `_proxy.py` and `_trivial.py` files are fine I guess, though
there's not much to them. (By "fine" I mean "looks fine at a glance,
but I've never even tried importing any of this code".)

The interesting part is creating our `Socket` class. The `_socket.py`
file has several half-written attempts jammed together, and lots of
notes in comments. They get closer to workable as you go down in the
file. (Don't trust everything you read there, it's a log of thoughts,
not conclusions.)

Anyway, the major challenging thing about supporting zmq in trio is
that zmq only grudgingly admits the idea that you might want to do
anything in your process besides use zmq. But we have to convince it
to play nicely with trio's event loop. (In fact this is so annoying
that pyzmq's asyncio integration involves using a custom asyncio event
loop based on zeromq. This is kind of terrible though and trio does
not support it so we have to grit our teeth and use the annoying
thing.)

So, here's how zmq's event loop integration works: You perform various
operations on your zmq "socket" as normal, making sure to use the
`NOBLOCK` mode. Sometimes, `send` or `recv` raises `zmq.Again`. Then
you need to block waiting for the zmq "socket" to become
readable/writable. At any given moment, you can check whether it's
readable/writable by calling `getsockopt(zmq.EVENTS)`. So far so
good. But how do we block waiting for readable/writable status to
change?

Well, there is a file descriptor they expose, as the `.fd` value on
the zmq socket. Maybe this is a shorthand for a call to `getsockopt`
or something? It seems like everything in zmq is a `getsockopt`.
Anyway, there's a file descriptor. The thing to realize about this
file descriptor is that its semantics are extremely counterintuitive.
It wasn't designed to be used this way; they just took an fd they use
internally and exposed it like "good luck have fun".

As far as I can tell, the semantics are this: every time the value of
`getsockopt(zmq.EVENTS)` becomes stale/out-of-date, then the fd
becomes readable, as a signal that zmq needs to do something
internally to update the `getsockopt(zmq.EVENTS)` value. So if you've
just looked at `getsockopt(zmq.EVENTS)` and it wasn't what you wanted,
you can block waiting for the fd to become readable. However, since
zmq also uses this fd internally, *pretty much any zmq operation might
update the internal cache of `getsockopt(zmq.EVENTS)` and mark the fd
non-readable again*. In particular, `send` and `recv` do this, as does
calling `getsockopt(zmq.EVENTS)`. But so can random other operations –
one that's mentioned in `zmq/green/core.py` is that
`setsockopt(SUBSCRIBE, ...)` and `setsockopt(UNSUBSCRIBE, ...)` can do
it, and there are some notes at the bottom of `_socket.py` of my
trying to trace through the zmq source code to figure out what other
operations might do it, but basically this is a fool's errand and the
only safe thing to do is assume that *any* call into zmq might trigger
a reset of the fd's state.

So this means that if one task tries to call `send`, and gets
`zmq.Again`, and then blocks waiting for the fd to become readable...
and then another task just randomly like, checks some socket option or
something, then this might cause zeromq to notice that actually the
socket is writable now *but our blocked task won't wake up* because
the notification got consumed by the other task.

Also, if you want to use a socket in duplex mode, with one task
calling `send` while the other calls `recv`, then this is complicated
because there's just one fd that's used for both, so you have to pick
one of them to do the waiting, and then maybe wake up the other.

At the bottom of `_socket.py` is some very complicated code that tries
to handle a bunch of these cases. It may be over-complicated. In
particular it shouldn't support having multiple tasks blocked in
`send` at the same time, or ditto for `recv`. That would be a small
win. A much bigger win would be if we made a rule that only one task
is allowed to use a given zmq socket at a time. (So in particular,
disallow concurrent calls to `send` and `recv` on the same socket.)
And this actually handles a lot of zmq's socket modes; a version of
this library with this restriction would still be useful for a *lot*
of zmq's use cases. But maybe not all, like I think there are some
cases where you might want to go full-duplex on ROUTER or DEALER
sockets? Anyway, we might want to start there.

As a general note, I started out thinking we'd want our `Socket` class
to inherit from pyzmq's, but now I think that was a bad idea and we
should just re-export the parts we want. (And possibly skip
re-exporting some parts, like the many redundant ways to call
`getsockopt()`.) You should expect to spend some time reading through
the definition of `Socket` in the pyzmq source to see what all it
exposes.

So yeah, that's what I know. This is not a working library. It needs a
single implementation of `Socket`, with more of the API exposed, it
needs tests, and docs, and all that good stuff. But it's a start!


# License

MIT/Apache 2 dual, Same as Trio, we should add proper license files.
And proper everything else files. By running cookiecutter-trio I
guess.
