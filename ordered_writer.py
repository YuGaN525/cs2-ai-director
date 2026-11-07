"""Bounded ordered IO, explicit acceptance and retryable shutdown.

Accepted operations remain in memory until completed. A failed operation stays
at the head; later operations cannot overtake it. close() reports whether every
accepted operation reached storage and may be retried after a storage failure.
"""
import queue
import threading
import time


class OrderedWriter:
    def __init__(self, capacity=2048):
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.current = None
        self.accepted = self.completed = self.rejected = 0
        self.error = ''
        self.last_write_ms = 0.
        self.accepting = True
        self.start()

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.accepting = True
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name='timeline-writer', daemon=True)
            self.thread.start()

    def offer(self, operation):
        with self.lock:
            if not self.accepting:
                self.rejected += 1
                return False
            try:
                self.queue.put_nowait(operation)
            except queue.Full:
                self.rejected += 1
                return False
            self.accepted += 1
            return True

    def _run(self):
        while True:
            if self.current is None:
                try:
                    self.current = self.queue.get(timeout=.1)
                except queue.Empty:
                    if self.stop_event.is_set():
                        return
                    continue
            started = time.perf_counter()
            try:
                self.current()
            except Exception:
                self.error = 'write_failed'
                if self.stop_event.wait(.5):
                    return
                continue
            self.last_write_ms = (time.perf_counter()-started)*1000
            self.completed += 1
            self.error = ''
            self.current = None

    def close(self, timeout=3):
        with self.lock:
            self.accepting = False
            self.stop_event.set()
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return not (thread and thread.is_alive()) and self.current is None and self.queue.empty()

    def stats(self):
        return {'accepted':self.accepted,'persisted':self.completed,
                'buffered':self.accepted-self.completed,'not_accepted':self.rejected,
                'error':self.error,'last_write_ms':self.last_write_ms}
