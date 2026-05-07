"""In-memory session store for active voice calls.

Each call gets a CallSession that holds:
  - identifiers (call_id, payer_id, invoice_id)
  - per-payer context loaded from DB at call start
  - the (phase, tone) call_state (mutated each turn)
  - the transition log (appended each turn)

The store is thread-safe so multiple Vapi calls can share the same
process. For now we keep sessions in-process — restart drops in-flight
calls. Persistence belongs in the next iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from voice_agent.state_machine import PayerContext, State


@dataclass
class CallSession:
    call_id: str
    payer_id: str
    invoice_id: str | None
    invoice_facts: str
    memory_summary: str
    payer_context: PayerContext
    call_state: State
    transition_log: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    # Most recent async compliance verdict for the assistant utterance the
    # caller just heard. None until the streaming path runs once.
    last_compliance: dict | None = None


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, CallSession] = {}
        self._lock = Lock()

    def get(self, call_id: str) -> CallSession | None:
        with self._lock:
            return self._sessions.get(call_id)

    def put(self, session: CallSession) -> None:
        with self._lock:
            self._sessions[session.call_id] = session

    def remove(self, call_id: str) -> None:
        with self._lock:
            self._sessions.pop(call_id, None)

    def all_call_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())
