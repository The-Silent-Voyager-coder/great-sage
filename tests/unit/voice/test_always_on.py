"""Always-on voice loop tests: wake gating, idle return, barge-in, bounds.

Fully offline: every I/O callable is a fake; no mic, no audio devices, no
model inference. The loop logic (state machine, barge polling, bounds) is
the tested contract; `chat.py` wires the real hardware.
"""

from __future__ import annotations

import threading
import time

from greatsage.voice.always_on import AlwaysOnVoiceLoop, LoopMode, StopLoop


class _Scripted:
    """Recording fakes for every loop callable."""

    def __init__(
        self,
        *,
        listens: list[str | None],
        wake_phrase: str = "great sage",
        replies: dict[str, str] | None = None,
        exits: set[str] | None = None,
    ) -> None:
        self.listens = list(listens)
        self.wake_phrase = wake_phrase
        self.replies = replies or {}
        self.exits = exits or set()
        self.spoken: list[str] = []
        self.replied: list[str] = []
        self.modes: list[LoopMode] = []
        self.stopped = 0

    def listen(self) -> str | None:
        return self.listens.pop(0) if self.listens else None

    def wake_check(self, text: str) -> tuple[bool, str]:
        lowered = text.lower()
        if self.wake_phrase in lowered:
            index = lowered.index(self.wake_phrase)
            remainder = (text[:index] + text[index + len(self.wake_phrase) :]).strip(" ,.!?")
            return (True, remainder)
        return (False, "")

    def reply(self, text: str) -> str | None:
        self.replied.append(text)
        return self.replies.get(text, f"answer to {text}")

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop_playback(self) -> None:
        self.stopped += 1

    def barge_detected(self) -> bool:
        return False

    def is_exit(self, text: str) -> bool:
        return text.lower().strip() in self.exits

    def on_mode(self, mode: LoopMode) -> None:
        self.modes.append(mode)


def _loop(script: _Scripted, **kwargs: object) -> AlwaysOnVoiceLoop:
    return AlwaysOnVoiceLoop(
        listen=script.listen,
        wake_check=script.wake_check,
        reply=script.reply,
        speak=script.speak,
        stop_playback=script.stop_playback,
        barge_detected=script.barge_detected,
        is_exit=script.is_exit,
        on_mode=script.on_mode,
        poll_seconds=0.01,
        **kwargs,  # type: ignore[arg-type]
    )


def test_wake_gates_conversation() -> None:
    script = _Scripted(
        listens=["hello there", "GREAT SAGE", "what time is it"],
        exits={"exit"},
    )
    _loop(script).run()
    # The non-wake window is ignored; the wake window only entered the mode;
    # the command turn was replied and spoken.
    assert script.replied == ["what time is it"]
    assert script.spoken == ["answer to what time is it"]
    assert LoopMode.CONVERSING in script.modes


def test_wake_and_command_in_one_utterance() -> None:
    script = _Scripted(listens=["great sage, what is two plus two"])
    _loop(script, max_turns=1).run()
    assert script.replied == ["what is two plus two"]  # wake phrase stripped
    assert script.spoken == ["answer to what is two plus two"]


def test_idle_turns_return_to_wake_mode() -> None:
    script = _Scripted(
        listens=["great sage", None, None, "great sage again"],
        exits=set(),
    )
    result = _loop(script, max_idle_turns=2).run()
    # Exact transition sequence: enter CONVERSING on wake, back to WAKE after
    # two speechless turns, re-enter on the next wake, and stop when the
    # empty-window bound is reached (no more windows in the script).
    assert script.modes == [
        LoopMode.CONVERSING,
        LoopMode.WAKE,
        LoopMode.CONVERSING,
        LoopMode.WAKE,
        LoopMode.STOPPED,
    ]
    assert result is LoopMode.STOPPED


def test_exit_command_stops_conversation() -> None:
    script = _Scripted(
        listens=["great sage", "do a thing", "exit", "never reached"],
        exits={"exit"},
    )
    _loop(script).run()
    assert script.replied == ["do a thing"]
    assert "never reached" not in script.replied
    assert LoopMode.STOPPED in script.modes


def test_max_turns_bound() -> None:
    script = _Scripted(
        listens=["great sage", "one", "two", "three", "four"],
    )
    _loop(script, max_turns=2).run()
    assert len(script.replied) == 2


def test_empty_windows_bound() -> None:
    script = _Scripted(listens=[None] * 4)
    result = _loop(script, max_empty_windows=3).run()
    assert result is LoopMode.STOPPED
    assert script.replied == []


def test_stop_loop_exception_ends_honestly() -> None:
    calls = {"count": 0}

    def listen() -> str | None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise StopLoop("voice session budget exhausted")
        return None

    script = _Scripted(listens=[])
    loop = AlwaysOnVoiceLoop(
        listen=listen,
        wake_check=script.wake_check,
        reply=script.reply,
        speak=script.speak,
        stop_playback=script.stop_playback,
        barge_detected=script.barge_detected,
    )
    assert loop.run() is LoopMode.STOPPED


def test_barge_in_interrupts_playback() -> None:
    script = _Scripted(listens=["great sage", "tell me a story", "exit"], exits={"exit"})
    speak_done = threading.Event()
    stop_calls = {"count": 0}

    def speak(text: str) -> None:
        script.spoken.append(text)
        speak_done.wait(timeout=2.0)  # blocks until the barge stops it

    def barge_detected() -> bool:
        return speak_done.is_set() is False and time.monotonic() > triggered["at"]

    def stop_playback() -> None:
        stop_calls["count"] += 1
        speak_done.set()  # simulate sd.stop() unblocking playback

    triggered = {"at": 0.0}
    loop = AlwaysOnVoiceLoop(
        listen=script.listen,
        wake_check=script.wake_check,
        reply=script.reply,
        speak=speak,
        stop_playback=stop_playback,
        barge_detected=barge_detected,
        is_exit=script.is_exit,
        poll_seconds=0.01,
    )
    triggered["at"] = time.monotonic() + 0.05  # barge once playback starts
    result = loop.run()
    assert stop_calls["count"] == 1  # playback was interrupted exactly once
    assert script.spoken == ["answer to tell me a story"]
    assert "exit" not in script.replied
    assert result is LoopMode.STOPPED


def test_speak_failure_is_isolated() -> None:
    script = _Scripted(listens=["great sage", "boom", "exit"], exits={"exit"})

    def speak(text: str) -> None:
        raise RuntimeError("audio device vanished")

    loop = AlwaysOnVoiceLoop(
        listen=script.listen,
        wake_check=script.wake_check,
        reply=script.reply,
        speak=speak,
        stop_playback=script.stop_playback,
        barge_detected=script.barge_detected,
        is_exit=script.is_exit,
        poll_seconds=0.01,
    )
    loop.run()  # must not raise; the conversation continues after the failure
    assert script.replied == ["boom"]
