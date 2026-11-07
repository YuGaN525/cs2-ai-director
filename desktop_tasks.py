"""Background work with main-thread result delivery and bounded UI draining."""
import queue
import threading
from desktop_ui import schedule


class Tasks:
    def __init__(self, root):
        self.root = root
        self.completed = queue.Queue()
        self.running = set()
        self.closed = False
        root.bind('<Destroy>', self._destroy, add='+')
        schedule(root, 50, self._poll)

    def _destroy(self, event):
        if event.widget is self.root:
            self.closed = True

    def submit(self, key, work, done):
        if self.closed or key in self.running:
            return False
        self.running.add(key)
        def run():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            if not self.closed:
                self.completed.put((key, done, result, error))
        threading.Thread(target=run, name='desktop-'+key, daemon=True).start()
        return True

    def _poll(self):
        for _ in range(32):
            try:
                key, done, result, error = self.completed.get_nowait()
            except queue.Empty:
                break
            self.running.discard(key)
            try:
                done(result, error)
            except Exception as exc:
                self.root.report_callback_exception(type(exc), exc, exc.__traceback__)
        if not self.closed:
            schedule(self.root, 50, self._poll)
