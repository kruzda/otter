"""Tests for otter.util.zk"""

import time
import uuid

from functools import partial

import attr

from characteristic import attributes

from effect import (
    ComposedDispatcher, Constant, Delay, Effect, Error, Func, TypeDispatcher,
    base_dispatcher, sync_perform)
from effect.testing import (
    SequenceDispatcher, const, conste, intent_func, noop, perform_sequence)

from kazoo.exceptions import (
    BadVersionError, LockTimeout, NoNodeError, NodeExistsError,
    SessionExpiredError)

from twisted.internet.defer import fail, maybeDeferred, succeed
from twisted.trial.unittest import SynchronousTestCase

from otter.test.utils import exp_func, mock_log, test_dispatcher
from otter.util import zk
from otter.util.zk import (
    CreateOrSet, CreateOrSetLoopLimitReachedError,
    DeleteNode, GetChildren, GetChildrenWithStats,
    GetStat,
    get_zk_dispatcher,
    perform_create_or_set, perform_delete_node)


@attributes(['version'])
class ZNodeStatStub(object):
    """Like a :obj:`ZnodeStat`, but only supporting the data we need."""


class ZKCrudModel(object):
    """
    A simplified model of Kazoo's CRUD operations, supporting
    version-check-and-set.

    To facilitate testing tricky concurrent scenarios, a system of 'post-hooks'
    is provided, which allows calling an arbitrary function immediately after
    some operations take effect.
    """
    def __init__(self):
        self.nodes = {}
        self.create_makepath = True

    def create(self, path, value="", acl=None, ephemeral=False, sequence=False,
               makepath=False):
        """Create a node."""
        assert makepath == self.create_makepath
        if path in self.nodes:
            return fail(NodeExistsError("{} already exists".format(path)))
        self.nodes[path] = (value, 0)
        return succeed(path)

    def get(self, path):
        """Get content of the node, and stat info."""
        if path not in self.nodes:
            return fail(NoNodeError("{} does not exist".format(path)))
        content, version = self.nodes[path]
        return succeed((content, ZNodeStatStub(version=version)))

    def _check_version(self, path, version):
        if path not in self.nodes:
            return fail(NoNodeError("{} does not exist".format(path)))
        if version != -1:
            current_version = self.nodes[path][1]
            if current_version != version:
                return fail(BadVersionError(
                    "When operating on {}, version {} was specified by "
                    "version {} was found".format(path, version,
                                                  current_version)))

    def set(self, path, new_value, version=-1):
        """Set the content of a node."""
        check = self._check_version(path, version)
        if check is not None:
            return check
        current_version = self.nodes[path][1]
        new_stat = ZNodeStatStub(version=current_version + 1)
        self.nodes[path] = (new_value, new_stat.version)
        return succeed(new_stat)

    def delete(self, path, version=-1):
        """Delete a node."""
        check = self._check_version(path, version)
        if check is not None:
            return check
        del self.nodes[path]
        return succeed('delete return value')

    def exists(self, path):
        """Return a ZnodeStat for a node if it exists, otherwise None."""
        if path in self.nodes:
            return ZNodeStatStub(version=self.nodes[path][1])
        else:
            return None


class _ZKLock(object):
    """
    Stub for :obj:`kazoo.recipe.lock.KazooLock` and :obj:`PollingLock`.
    It provides *_eff implementations based on ``LockBehavior`` for
    ``PollingLock``

    This class is private. Get its object and control it by calling
    ``create_fake_lock``
    """
    def __init__(self, behavior):
        self._behavior = behavior

    def is_acquired(self):
        return succeed(self._behavior.acquired)

    def is_acquired_eff(self):
        return Effect(Constant(self._behavior.acquired))

    def acquire_eff(self, blocking, timeout):
        assert (self._behavior.acquired is LockBehavior.NOT_STARTED or
                (not self._behavior.acquired))
        assert (blocking, timeout) == self._behavior.acquire_call[:2]
        ret = self._behavior.acquire_call[-1]
        if isinstance(ret, Exception):
            self._behavior.acquired = False
            return Effect(Error(ret))
        else:
            self._behavior.acquired = ret
            return Effect(Constant(ret))

    def _set_acquired(self, r, acquired):
        self._behavior.acquired = acquired
        return r

    def acquire(self, blocking=True, timeout=None):
        assert (self._behavior.acquired is LockBehavior.NOT_STARTED or
                (not self._behavior.acquired))
        assert (blocking, timeout) == self._behavior.acquire_call[:2]
        d = maybeDeferred(lambda: self._behavior.acquire_call[-1])
        return d.addCallback(self._set_acquired, True)

    def release(self):
        d = maybeDeferred(lambda: self._behavior.release_call)
        return d.addCallback(self._set_acquired, False)


@attr.s
class LockBehavior(object):
    """
    Use this class to control behavior of ``_ZKLock`` object
    """
    # tuple of (blocking, timeout, return_value) to be set by test
    acquire_call = attr.ib()
    # release return value
    release_call = attr.ib()
    # SENTINEL object to represent the fact that lock has initialized but
    # not yet acquired
    NOT_STARTED = object()
    # Is lock acquired?
    acquired = attr.ib(default=NOT_STARTED)


def create_fake_lock(acquire_call=None, release_call=None):
    """
    Create fake ZKLock object and return it along with its behavior class
    """
    b = LockBehavior(acquire_call, release_call)
    return b, _ZKLock(b)


class CreateOrSetTests(SynchronousTestCase):
    """Tests for :func:`create_or_set`."""
    def setUp(self):
        self.model = ZKCrudModel()

    def _cos(self, path, content):
        eff = Effect(CreateOrSet(path=path, content=content))
        performer = partial(perform_create_or_set, self.model)
        dispatcher = TypeDispatcher({CreateOrSet: performer})
        return sync_perform(dispatcher, eff)

    def test_create(self):
        """Creates a node when it doesn't exist."""
        result = self._cos('/foo', 'bar')
        self.assertEqual(result, '/foo')
        self.assertEqual(self.model.nodes, {'/foo': ('bar', 0)})

    def test_update(self):
        """Uses `set` to update the node when it does exist."""
        self.model.create('/foo', 'initial', makepath=True)
        result = self._cos('/foo', 'bar')
        self.assertEqual(result, '/foo')
        self.assertEqual(self.model.nodes, {'/foo': ('bar', 1)})

    def test_node_disappears_during_update(self):
        """
        If `set` can't find the node (because it was unexpectedly deleted
        between the `create` and `set` calls), creation will be retried.
        """
        def hacked_set(path, value):
            self.model.delete('/foo')
            del self.model.set  # Only let this behavior run once
            return self.model.set(path, value)
        self.model.set = hacked_set

        self.model.create('/foo', 'initial', makepath=True)
        result = self._cos('/foo', 'bar')
        self.assertEqual(result, '/foo')
        # It must be at version 0 because it's a creation, whereas normally if
        # the node were being updated it'd be at version 1.
        self.assertEqual(self.model.nodes, {'/foo': ('bar', 0)})

    def test_loop_limit(self):
        """
        performing a :obj:`CreateOrSet` will avoid infinitely looping in
        pathological cases, and eventually blow up with a
        :obj:`CreateOrSetLoopLimitReachedError`.
        """
        def hacked_set(path, value):
            return fail(NoNodeError())

        def hacked_create(path, content, makepath):
            return fail(NodeExistsError())

        self.model.set = hacked_set
        self.model.create = hacked_create

        exc = self.assertRaises(CreateOrSetLoopLimitReachedError,
                                self._cos, '/foo', 'bar')
        self.assertEqual(str(exc), '/foo')


class GetChildrenWithStatsTests(SynchronousTestCase):
    """Tests for :func:`get_children_with_stats`."""
    def setUp(self):
        # It'd be nice if we used the standard ZK CRUD model, but implementing
        # a tree of nodes supporting get_children is a pain
        class Model(object):
            pass
        self.model = Model()

    def _gcws(self, path):
        eff = Effect(GetChildrenWithStats(path))
        dispatcher = ComposedDispatcher([test_dispatcher(),
                                         get_zk_dispatcher(self.model)])
        return sync_perform(dispatcher, eff)

    def test_get_children_with_stats(self):
        """
        get_children_with_stats returns path of all children along with their
        ZnodeStat objects. Any children that disappear between ``get_children``
        and ``exists`` are not returned.
        """
        def exists(p):
            if p == '/path/foo':
                return succeed(ZNodeStatStub(version=0))
            if p == '/path/bar':
                return succeed(ZNodeStatStub(version=1))
            if p == '/path/baz':
                return succeed(None)
        self.model.get_children = {'/path': succeed(['foo', 'bar', 'baz'])}.get
        self.model.exists = exists

        result = self._gcws('/path')
        self.assertEqual(result,
                         [('foo', ZNodeStatStub(version=0)),
                          ('bar', ZNodeStatStub(version=1))])


class GetChildrenTests(SynchronousTestCase):
    """Tests for :obj:`GetChildren`."""

    def setUp(self):
        # It'd be nice if we used the standard ZK CRUD model, but implementing
        # a tree of nodes supporting get_children is a pain
        class Model(object):
            pass
        self.model = Model()

    def _gc(self, path):
        eff = Effect(GetChildren(path))
        dispatcher = get_zk_dispatcher(self.model)
        return sync_perform(dispatcher, eff)

    def test_get_children(self):
        """Returns children."""
        self.model.get_children = {'/path': succeed(['foo', 'bar', 'baz'])}.get

        result = self._gc('/path')
        self.assertEqual(result,
                         ['foo', 'bar', 'baz'])


class GetStatTests(SynchronousTestCase):
    def setUp(self):
        self.model = ZKCrudModel()

    def _gs(self, path):
        eff = Effect(GetStat(path))
        dispatcher = get_zk_dispatcher(self.model)
        return sync_perform(dispatcher, eff)

    def test_get_stat(self):
        """Returns the ZnodeStat when the node exists."""
        self.model.create('/foo/bar', value='foo', makepath=True)
        result = self._gs('/foo/bar')
        self.assertEqual(result, ZNodeStatStub(version=0))

    def test_get_stat_not_exists(self):
        """Returns None when no node exists."""
        result = self._gs('/foo/bar')
        self.assertEqual(result, None)


class DeleteTests(SynchronousTestCase):
    """Tests for :obj:`DeleteNode`."""
    def test_delete(self):
        model = ZKCrudModel()
        eff = Effect(DeleteNode(path='/foo', version=1))
        model.create('/foo', 'initial', makepath=True)
        model.set('/foo', 'bar')
        performer = partial(perform_delete_node, model)
        dispatcher = TypeDispatcher({DeleteNode: performer})
        result = sync_perform(dispatcher, eff)
        self.assertEqual(model.nodes, {})
        self.assertEqual(result, 'delete return value')


class CreateTests(SynchronousTestCase):
    """Tests for :obj:`CreateNode`."""
    def test_create(self):
        model = ZKCrudModel()
        model.create_makepath = False
        eff = Effect(zk.CreateNode(path='/foo', value="v"))
        dispatcher = get_zk_dispatcher(model)
        result = sync_perform(dispatcher, eff)
        self.assertEqual(model.nodes, {"/foo": ("v", 0)})
        self.assertEqual(result, '/foo')


class PollingLockTests(SynchronousTestCase):

    def setUp(self):
        self.lock = zk.PollingLock("disp", "/testlock", "id", 0.1)

    def test_acquire_success(self):
        """
        acquire_eff creates child and gets lock as it is the smallest one
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), conste(NodeExistsError())),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000000")),
            (GetChildren("/testlock"), const(["prefix0000000000"]))
        ]
        self.assertTrue(
            perform_sequence(seq, self.lock.acquire_eff(False, None)))

    def test_acquire_create_path_success(self):
        """
        acquire_eff creates provided path if it doesn't exist
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000000")),
            (GetChildren("/testlock"), const(["prefix0000000000"]))
        ]
        self.assertTrue(
            perform_sequence(seq, self.lock.acquire_eff(False, None)))

    def test_acquire_delete_child(self):
        """
        acquire_eff deletes existing child if it exists
        """
        self.lock._node = "/testlock/prefix000000002"
        seq = [
            (DeleteNode(path="/testlock/prefix000000002", version=-1), noop),
            (zk.CreateNode("/testlock"), conste(NodeExistsError())),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000000")),
            (GetChildren("/testlock"), const(["prefix0000000000"]))
        ]
        self.assertTrue(
            perform_sequence(seq, self.lock.acquire_eff(False, None)))

    def test_acquire_blocking_success(self):
        """
        acquire_eff creates child, realizes its not the smallest. Tries again
        every 0.01 seconds until it succeeds
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000001")),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0)),
            (Delay(0.1), noop),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0.2)),
            (Delay(0.1), noop),
            (GetChildren("/testlock"), const(["prefix0000000001"]))
        ]
        self.assertTrue(
            perform_sequence(seq, self.lock.acquire_eff(True, 1)))

    def test_acquire_blocking_no_timeout(self):
        """
        When acquire_eff is called without timeout, it creates child, realizes
        its not the smallest, tries again every 0.1 seconds without checking
        time and succeeds if its the smallest node
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000001")),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0)),
            (Delay(0.1), noop),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Delay(0.1), noop),
            (GetChildren("/testlock"), const(["prefix0000000001"]))
        ]
        self.assertTrue(
            perform_sequence(seq, self.lock.acquire_eff(True, None)))

    def test_acquire_nonblocking_fails(self):
        """
        acquire_eff creates child and returns False immediately after finding
        its not the smallest child when blocking=False. It deletes child node
        before returning.
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000001")),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (DeleteNode(path="/testlock/prefix0000000001", version=-1), noop)
        ]
        self.assertFalse(
            perform_sequence(seq, self.lock.acquire_eff(False, None)))

    def test_acquire_timeout(self):
        """
        acquire_eff creates child node and keeps checking if it is smallest and
        eventually gives up by raising `LockTimeout`. It deletes child node
        before returning.
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000001")),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0)),
            (Delay(0.1), noop),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0.12)),
            (Delay(0.1), noop),
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"])),
            (Func(time.time), const(0.4)),
            (DeleteNode(path="/testlock/prefix0000000001", version=-1), noop)
        ]
        self.assertRaises(
            LockTimeout, perform_sequence, seq,
            self.lock.acquire_eff(True, 0.3))

    def test_acquire_other_error(self):
        """
        If acquire_eff internally raises any error then it tries to delete
        child node before returning.
        """
        seq = [
            (Constant(None), noop),
            (zk.CreateNode("/testlock"), const("/testlock")),
            (Func(uuid.uuid4), const("prefix")),
            (zk.CreateNode(
                "/testlock/prefix", value="id",
                ephemeral=True, sequence=True),
             const("/testlock/prefix0000000001")),
            (GetChildren("/testlock"), conste(SessionExpiredError())),
            (DeleteNode(path="/testlock/prefix0000000001", version=-1),
             conste(SessionExpiredError()))
        ]
        self.assertRaises(
            SessionExpiredError, perform_sequence, seq,
            self.lock.acquire_eff(True, 0.3))

    def test_is_acquired_no_node(self):
        """
        is_acquired_eff returns False if there is no child node
        """
        self.assertFalse(perform_sequence([], self.lock.is_acquired_eff()))

    def test_is_acquired_no_children(self):
        """
        is_acquired_eff returns False if there are no children
        """
        self.lock._node = "/testlock/prefix000000000"
        seq = [(GetChildren("/testlock"), const([]))]
        self.assertFalse(perform_sequence(seq, self.lock.is_acquired_eff()))

    def test_is_acquired_first_child(self):
        """
        is_acquired_eff returns True if it's node is the first child
        """
        self.lock._node = "/testlock/prefix0000000000"
        seq = [
            (GetChildren("/testlock"),
             const(["prefix0000000001", "prefix0000000000"]))
        ]
        self.assertTrue(perform_sequence(seq, self.lock.is_acquired_eff()))

    def test_is_acquired_not_first_child(self):
        """
        is_acquired_eff returns False if its not is not the first child
        """
        self.lock._node = "/testlock/prefix0000000001"
        seq = [
            (GetChildren("/testlock"),
             const(["prefix0000000000", "prefix0000000001"]))
        ]
        self.assertFalse(perform_sequence(seq, self.lock.is_acquired_eff()))

    def test_release_deletes_child(self):
        """
        release_eff deletes child stored in self._node and sets it to None
        after deleting
        """
        self.lock._node = "/testlock/prefix0000000001"
        seq = [(DeleteNode(path=self.lock._node, version=-1), noop)]
        self.assertIsNone(perform_sequence(seq, self.lock.release_eff()))
        self.assertIsNone(self.lock._node)

    def test_release_nonodeerror(self):
        """
        release_eff deletes child stored in self._node and sets it to None
        if delete raises NoNodeError
        """
        self.lock._node = "/testlock/prefix0000000001"
        seq = [
            (DeleteNode(path=self.lock._node, version=-1),
             conste(NoNodeError()))]
        self.assertIsNone(perform_sequence(seq, self.lock.release_eff()))
        self.assertIsNone(self.lock._node)

    def test_release_no_node_reset(self):
        """
        If release_eff fails to delete child node, it will not set self._node
        to None
        """
        node = self.lock._node = "/testlock/prefix0000000001"
        seq = [(DeleteNode(path=self.lock._node, version=-1),
                conste(SessionExpiredError()))]
        self.assertRaises(
            SessionExpiredError, perform_sequence, seq,
            self.lock.release_eff())
        self.assertIs(self.lock._node, node)

    def test_release_does_nothing(self):
        """
        If self._node is None, release does nothing
        """
        self.assertIsNone(perform_sequence([], self.lock.release_eff()))

    def test_acquire_performs(self):
        """
        acquire performs effect from acquire_eff
        """
        self.lock.dispatcher = SequenceDispatcher([
            (("acquire", "blocking", "timeout"), const("ret"))])
        self.lock.acquire_eff = intent_func("acquire")
        self.assertEqual(
            self.successResultOf(self.lock.acquire("blocking", "timeout")),
            "ret")

    def test_release_performs(self):
        """
        release performs effect from release_eff
        """
        self.lock.dispatcher = SequenceDispatcher([
            (("release",), const("ret"))])
        self.lock.release_eff = intent_func("release")
        self.assertEqual(self.successResultOf(self.lock.release()), "ret")

    def test_is_acquired_performs(self):
        """
        is_acquired performs effect from is_acquired_eff
        """
        self.lock.dispatcher = SequenceDispatcher([
            (("is_acquired",), const("ret"))])
        self.lock.is_acquired_eff = intent_func("is_acquired")
        self.assertEqual(self.successResultOf(self.lock.is_acquired()), "ret")


class CallIfAcquiredTests(SynchronousTestCase):
    """
    Tests for :func:`call_if_acquired`
    """
    def setUp(self):
        self.lb, self.lock = create_fake_lock()

    def test_lock_not_acquired(self):
        """
        When lock is not acquired, it is tried and if failed does not
        call eff
        """
        self.lb.acquired = False
        self.lb.acquire_call = (False, None, False)
        self.assertEqual(
            sync_perform(
                base_dispatcher,
                zk.call_if_acquired(self.lock, Effect("call"))),
            (zk.NOT_CALLED, False))

    def test_lock_acquired(self):
        """
        When lock is not acquired, it is tried and if successful calls eff
        """
        self.lb.acquired = False
        self.lb.acquire_call = (False, None, True)
        seq = [("call", const("eff_return"))]
        self.assertEqual(
            perform_sequence(
                seq,
                zk.call_if_acquired(self.lock, Effect("call"))),
            ("eff_return", True))

    def test_lock_already_acquired(self):
        """
        If lock is already acquired, it will just call eff
        """
        self.lb.acquired = True
        seq = [("call", const("eff_return"))]
        self.assertEqual(
            perform_sequence(
                seq,
                zk.call_if_acquired(self.lock, Effect("call"))),
            ("eff_return", False))


class LockedTests(SynchronousTestCase):
    """
    Tests for :func:`locked`
    """

    def setUp(self):
        self.func = lambda: succeed("ret")
        self.lb, lock = create_fake_lock()
        self.lf = zk.locked(lock, base_dispatcher, self.func)

    def test_func_called_lock_already_acquired(self):
        """
        `self.func` is called if ``call_if_acquired`` returns the fact that
        lock is already acquired
        """
        self.lb.acquired = True
        d = self.lf()
        self.assertEqual(self.successResultOf(d), ("ret", False))

    def test_func_called_lock_acquired(self):
        """
        ``self.func`` is called if ``call_if_acquired`` calls effect after
        acquiring the lock and acquired message is logged
        """
        self.lb.acquired = False
        self.lb.acquire_call = (False, None, True)
        d = self.lf()
        self.assertEqual(self.successResultOf(d), ("ret", True))

    def test_func_not_called(self):
        """
        ``self.func`` is not called if ``call_if_acquired`` returns lock not
        acquired
        """
        self.lb.acquired = False
        self.lb.acquire_call = (False, None, False)
        d = self.lf()
        self.assertEqual(self.successResultOf(d), (zk.NOT_CALLED, False))


class AddAcquiredLogTests(SynchronousTestCase):
    """
    Tests for :func:`add_acquired_log`
    """

    def setUp(self):
        self.log = mock_log()
        self.ret = ("r", True)
        self.func = lambda: succeed(self.ret)
        self.wf = zk.add_acquired_log(self.log, "m", self.func)

    def test_message_logged(self):
        """
        Message is logged when function returns True as second element. Returns
        first element.
        """
        d = self.wf()
        self.assertEqual(self.successResultOf(d), "r")
        self.log.msg.assert_called_once_with("m")

    def test_not_logged(self):
        """
        Message is not logged when function returns True as second element.
        Returns first element.
        """
        self.ret = ("r", False)
        d = self.wf()
        self.assertEqual(self.successResultOf(d), "r")
        self.assertFalse(self.log.msg.called)


class LockedLoggedFuncTests(SynchronousTestCase):
    """
    Tests for :func:`locked_logged_func`
    """

    def test_composition(self):
        """
        Ensure :func:`locked` and :func:`add_acquired_log` are called in
        sequence with correct parameters
        """
        self.patch(zk, "PollingLock", exp_func(self, "lock", "disp", "/path"))
        self.patch(zk, "locked",
                   exp_func(self, "locked_f", "lock", "disp", "func", 1))
        self.patch(zk, "add_acquired_log",
                   exp_func(self, "llf", "log", "msg", "locked_f"))

        wf, lock = zk.locked_logged_func("disp", "/path", "log", "msg",
                                         "func", 1)

        self.assertEqual(wf, "llf")
        self.assertEqual(lock, "lock")


class CreateHealthCheckTests(SynchronousTestCase):
    """
    Tests for :func:`create_health_check`
    """

    def setUp(self):
        self.lb, lock = create_fake_lock()
        self.health_check = zk.create_health_check(lock)

    def test_acquired(self):
        """
        Returned function returns True when lock is acquired
        """
        self.lb.acquired = True
        self.assertEqual(
            self.successResultOf(self.health_check()),
            (True, {"has_lock": True}))

    def test_not_acquired(self):
        """
        Returned function returns False when lock is not acquired
        """
        self.lb.acquired = False
        self.assertEqual(
            self.successResultOf(self.health_check()),
            (True, {"has_lock": False}))
