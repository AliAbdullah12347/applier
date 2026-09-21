"""Background tasks for the GUI, with live log streaming and clean cancellation.

The CLI can afford to block: you started it, you are watching it, Ctrl-C ends it.
A GUI cannot. `applier run` may sit there for hours, and the browser needs to
show what it is doing *while* it does it, and be able to stop it without killing
the process and losing the database write that was half-done.

So every long operation runs on a worker thread that:

  * writes its output into a bounded ring buffer the HTTP layer can tail,
  * checks a cooperative stop flag at each safe point rather than being killed,
  * records a terminal status, so a browser that reconnects after a refresh can
    still find out how the thing it started actually ended.

Cooperative cancellation matters more than it sounds. A submission is a network
side effect: killing a thread mid-POST would leave a row saying "preparing" for
an application the employer has actually received. The stop flag is only read
between applications, never inside one.
"""

from __future__ import annotations

import io
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

MAX_LINES = 4000          # ring buffer per task; a long run trims its own head
MAX_FINISHED = 40         # completed tasks kept for inspection


class Stopped(Exception):
    """Raised inside a worker when the user asked it to stop."""


@dataclass
class Task:
    id: str
    name: str
    kind: str
    status: str = "running"          # running | done | failed | stopped
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    result: Any = None
    error: str | None = None
    _lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    _seq: int = 0                    # monotonic line counter, survives trimming
    _stop: threading.Event = field(default_factory=threading.Event)
    _cv: threading.Condition = field(default_factory=threading.Condition)

    # ----------------------------------------------------------------- write
    def emit(self, text: str) -> None:
        """Append output. Splits on newlines so the browser gets whole lines."""
        if not text:
            return
        with self._cv:
            for piece in text.splitlines():
                self._seq += 1
                self._lines.append((self._seq, piece))
            self._cv.notify_all()

    def finish(self, status: str, *, result: Any = None, error: str | None = None) -> None:
        with self._cv:
            self.status = status
            self.result = result
            self.error = error
            self.ended_at = time.time()
            self._cv.notify_all()

    # ------------------------------------------------------------------ read
    def since(self, cursor: int) -> tuple[list[str], int]:
        """Lines newer than `cursor`, plus the new cursor.

        If the ring buffer has wrapped past the caller's cursor the caller has
        simply missed those lines; it resumes from the oldest line still held
        rather than replaying from zero.
        """
        with self._cv:
            out = [text for seq, text in self._lines if seq > cursor]
            newest = self._lines[-1][0] if self._lines else cursor
        return out, newest

    def wait_for_change(self, cursor: int, timeout: float) -> None:
        """Block until there is something new, the task ends, or timeout."""
        with self._cv:
            if self._lines and self._lines[-1][0] > cursor:
                return
            if self.status != "running":
                return
            self._cv.wait(timeout)

    # --------------------------------------------------------------- control
    def request_stop(self) -> None:
        self._stop.set()
        self.emit("[stop requested — finishing the current step safely]")
        with self._cv:
            self._cv.notify_all()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self) -> None:
        """Call at a safe point; raises if the user asked to stop."""
        if self._stop.is_set():
            raise Stopped()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": round((self.ended_at or time.time()) - self.started_at, 1),
            "result": self.result if isinstance(self.result, (str, int, float, dict, list, type(None))) else str(self.result),
            "error": self.error,
        }


class _TaskWriter(io.TextIOBase):
    """A file-like object so `rich.Console(file=...)` writes into a Task."""

    def __init__(self, task: Task) -> None:
        self._task = task
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        if "\n" in self._buf:
            head, _, self._buf = self._buf.rpartition("\n")
            self._task.emit(head)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._task.emit(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False


def console_for(task: Task):
    """A rich Console that renders into the task log.

    `force_terminal=False` and a fixed width keep ANSI escapes out of the
    stream — the browser renders plain text and a stray escape sequence would
    show up as mojibake rather than colour.
    """
    from rich.console import Console
    return Console(file=_TaskWriter(task), force_terminal=False, no_color=True,
                   width=100, soft_wrap=False)


class TaskRunner:
    """Owns every background task. One instance per server."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def start(self, name: str, kind: str, fn: Callable[[Task], Any],
              *, exclusive: bool = True) -> Task:
        """Run `fn(task)` on a worker thread.

        `exclusive` refuses a second task of the same kind while one is live.
        Two concurrent `apply` runs would drive the same persistent browser
        profile from two threads, which is a good way to submit half of one
        application into the form of another.
        """
        with self._lock:
            if exclusive:
                for t in self._tasks.values():
                    if t.kind == kind and t.status == "running":
                        raise RuntimeError(
                            f"a '{kind}' task is already running — stop it first")
            task = Task(id=uuid.uuid4().hex[:12], name=name, kind=kind)
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._trim()

        def runner() -> None:
            try:
                result = fn(task)
                task.finish("done", result=result)
            except Stopped:
                task.emit("stopped.")
                task.finish("stopped")
            except Exception as e:                    # noqa: BLE001 - surfaced to UI
                task.emit(f"ERROR: {e}")
                task.emit(traceback.format_exc(limit=6))
                task.finish("failed", error=str(e))

        threading.Thread(target=runner, name=f"applier-{kind}", daemon=True).start()
        return task

    def _trim(self) -> None:
        """Drop the oldest finished tasks; never drop a running one."""
        finished = [i for i in self._order
                    if self._tasks[i].status != "running"]
        while len(finished) > MAX_FINISHED:
            drop = finished.pop(0)
            self._tasks.pop(drop, None)
            self._order.remove(drop)

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._tasks[i].public() for i in reversed(self._order)
                    if i in self._tasks]

    def running(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == "running"]

    def stop_all(self) -> None:
        for t in self.running():
            t.request_stop()

    def stream(self, task: Task, cursor: int = 0) -> Iterator[str]:
        """Server-sent events: log lines, then a terminal status frame."""
        import json as _json

        lines, cursor = task.since(cursor)
        for ln in lines:
            yield f"data: {_json.dumps({'line': ln, 'cursor': cursor})}\n\n"

        last_ping = time.time()
        while True:
            task.wait_for_change(cursor, timeout=1.0)
            lines, cursor = task.since(cursor)
            for ln in lines:
                yield f"data: {_json.dumps({'line': ln, 'cursor': cursor})}\n\n"
            if task.status != "running":
                yield f"data: {_json.dumps({'done': True, 'task': task.public()})}\n\n"
                return
            if time.time() - last_ping > 15:
                # Comment frame: keeps proxies and the browser from timing out
                # an idle stream during the loop's long sleep between cycles.
                yield ": keepalive\n\n"
                last_ping = time.time()


RUNNER = TaskRunner()
