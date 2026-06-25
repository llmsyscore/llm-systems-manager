"""Per-run SSE replay buffer. Events get a monotonic seq within a run; the SSE
`id: <run_id>:<seq>` lets a reconnecting EventSource resume via Last-Event-ID.

Mirrored verbatim between agent/ and llm-systems-manager/backend/ — keep in sync.
"""
from collections import deque


class BenchReplayBuffer:
    def __init__(self, maxlen=5000):
        self._buf = deque(maxlen=maxlen)
        self._run_id = ""
        self._seq = 0

    @property
    def run_id(self):
        return self._run_id

    def start_run(self, run_id):
        """Begin a new run: reset seq and clear the buffer."""
        self._run_id = str(run_id)
        self._seq = 0
        self._buf.clear()

    def append(self, event):
        """Tag event with the next id, store it, and return the record
        {id, seq, event}. Buffer is bounded; oldest records evict."""
        self._seq += 1
        rec = {"id": f"{self._run_id}:{self._seq}", "seq": self._seq, "event": event}
        self._buf.append(rec)
        return rec

    def seq_for(self, last_event_id):
        """The seq a stream should resume after. A matching run returns that
        run's seq; empty/missing, run mismatch, or malformed all return 0
        (replay the whole current buffer)."""
        if not last_event_id:
            return 0
        run, sep, seq = str(last_event_id).partition(":")
        if not sep or run != self._run_id:
            return 0
        try:
            return int(seq)
        except ValueError:
            return 0

    def records_after_seq(self, seq):
        """Records in the current buffer with seq greater than the given seq."""
        return [r for r in self._buf if r["seq"] > seq]

    def replay_after(self, last_event_id):
        """Records to (re)send to a connecting client, resolved from its
        Last-Event-ID via seq_for + records_after_seq."""
        return self.records_after_seq(self.seq_for(last_event_id))
