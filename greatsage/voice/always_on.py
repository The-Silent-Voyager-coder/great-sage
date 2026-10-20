"""Always-on voice loop (roadmap gate 2): wake-word gating + barge-in.

The loop idles in WAKE mode transcribing bounded windows; a wake-word match
enters CONVERSING mode (an utterance may carry the command after the wake
phrase — "great sage, what time is it" works in one breath). After
``max_idle_turns`` speechless turns it returns to WAKE mode. While replies
are spoken, the loop polls the barge detector on a short interval and stops
playback the moment the operator speaks over it (barge-in), then keeps
listening.

Everything except the decision logic is injected — `listen`, `wake_check`,
`reply`, `speak`, `stop_playback`, `barge_detected` — so the loop is
deterministic and fully offline-testable; `greatsage/chat.py` wires the real
microphone, VoiceService (STT/TTS), and IntelligenceService. All waits are
bounded; a `StopLoop` raised by an injected callable (e.g. the voice session
budget is exhausted) ends the loop honestly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum

log = logging.getLogger("greatsage.voice.always_on")


class LoopMode(StrEnum):
    WAKE = "wake"
    CONVERSING = "conversing"
    STOPPED = "stopped"


class StopLoop(Exception):
    """Raised by an injected callable to end the loop (bounded, honest)."""


class AlwaysOnVoiceLoop:
    """Wake-gated voice conversation loop with barge-in.

    Callables (all required unless noted):

    - ``listen() -> str | None``: one bounded utterance transcript; ``None``
      when no speech was captured (or the window was empty).
    - ``wake_check(text) -> tuple[bool, str]``: whether the text carries the
      wake phrase, plus the remainder after it.
    - ``reply(text) -> str | None``: the assistant answer (``None`` = none).
    - ``speak(text) -> None``: blocking playback of one reply.
    - ``stop_playback() -> None``: interrupt the in-flight playback.
    - ``barge_detected() -> bool``: True while the operator is speaking over
      the reply.
    - ``is_exit(text) -> bool`` (optional): end-command check.
    - ``on_mode(mode) -> None`` (optional): status transitions.
    """

    def __init__(
        self,
        *,
        listen: Callable[[], str | None],
        wake_check: Callable[[str], tuple[bool, str]],
        reply: Callable[[str], str | None],
        speak: Callable[[str], None],
        stop_playback: Callable[[], None],
        barge_detected: Callable[[], bool],
        is_exit: Callable[[str], bool] | None = None,
        on_mode: Callable[[LoopMode], None] | None = None,
        max_turns: int = 100,
        max_idle_turns: int = 2,
        max_empty_windows: int = 50,
        poll_seconds: float = 0.05,
    ) -> None:
        self._listen = listen
        self._wake_check = wake_check
        self._reply = reply
        self._speak = speak
        self._stop_playback = stop_playback
        self._barge_detected = barge_detected
        self._is_exit = is_exit or (lambda _text: False)
        self._on_mode = on_mode
        self._max_turns = max(1, max_turns)
        self._max_idle_turns = max(1, max_idle_turns)
        self._max_empty_windows = max(1, max_empty_windows)
        self._poll = max(0.01, poll_seconds)

    # --- public ---------------------------------------------------------

    def run(self) -> LoopMode:
        """Run until exit/limit/StopLoop; returns the final mode."""
        mode = LoopMode.WAKE
        turns = 0
        idle_turns = 0
        empty_windows = 0
        try:
            while turns < self._max_turns:
                if mode is LoopMode.WAKE:
                    text = self._listen()
                    if text is None:
                        empty_windows += 1
                        if empty_windows >= self._max_empty_windows:
                            log.info("always-on: no speech for %d windows", empty_windows)
                            mode = LoopMode.STOPPED
                            break
                        continue
                    empty_windows = 0
                    detected, remainder = self._wake_check(text)
                    if not detected:
                        continue
                    mode = LoopMode.CONVERSING
                    idle_turns = 0
                    self._emit_mode(mode)
                    if remainder.strip():
                        # Wake phrase and command in one utterance.
                        if self._turn(remainder) is False:
                            mode = LoopMode.STOPPED
                            break
                        turns += 1
                    continue

                # CONVERSING
                text = self._listen()
                if text is None:
                    idle_turns += 1
                    if idle_turns >= self._max_idle_turns:
                        mode = LoopMode.WAKE
                        self._emit_mode(mode)
                    continue
                idle_turns = 0
                if self._turn(text) is False:
                    mode = LoopMode.STOPPED
                    break
                turns += 1
        except StopLoop as exc:
            log.info("always-on loop stopped: %s", exc)
            mode = LoopMode.STOPPED
        self._emit_mode(mode)
        return mode

    # --- internals ------------------------------------------------------

    def _turn(self, text: str) -> bool:
        """One conversing turn; False ends the loop (exit command)."""
        if self._is_exit(text):
            return False
        answer = self._reply(text)
        if not answer:
            return True
        if self._speak_with_barge_in(answer):
            log.info("barge-in: reply playback interrupted")
        return True

    def _speak_with_barge_in(self, text: str) -> bool:
        """Speak on a worker thread; poll the barge detector and interrupt.

        The detector is only polled; stopping playback is entirely the
        injected callable's job. Speak failures are isolated (logged), never
        fatal to the loop.
        """
        done = threading.Event()

        def worker() -> None:
            try:
                self._speak(text)
            except Exception as exc:  # playback is best-effort
                log.warning("always-on speak failed: %s", exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker, name="greatsage-speak", daemon=True)
        thread.start()
        barged = False
        while not done.is_set():
            if self._barge_detected():
                barged = True
                try:
                    self._stop_playback()
                except Exception as exc:  # stop is best-effort
                    log.debug("barge-in stop failed: %s", exc)
                done.wait(timeout=1.0)
                break
            done.wait(timeout=self._poll)
        return barged

    def _emit_mode(self, mode: LoopMode) -> None:
        if self._on_mode is not None:
            try:
                self._on_mode(mode)
            except Exception as exc:  # status callbacks never break the loop
                log.debug("mode callback failed: %s", exc)
