from repryntt.core.identity.hearing_runtime import AlwaysOnHearingRuntime


def test_listen_for_utterance_consumes_wake_word_transcript_as_turn():
    hearing = AlwaysOnHearingRuntime(["andrew"])
    hearing._running.set()

    hearing._emit_text("If you had arms Andrew, sure.", 1.2)

    result = hearing.listen_for_utterance(timeout=0.01)

    assert result["silence"] is False
    assert result["text"] == "If you had arms Andrew, sure."
    assert result["wake_word"] == "andrew"
    assert hearing.get_wake_event(timeout=0.0) is None


def test_clear_wake_events_discards_pending_wake_triggers():
    hearing = AlwaysOnHearingRuntime(["andrew"])

    hearing._emit_text("Andrew, are you there?", 0.8)
    hearing.clear_wake_events()

    assert hearing.get_wake_event(timeout=0.0) is None
