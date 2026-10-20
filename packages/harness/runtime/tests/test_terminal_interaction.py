from terminal_interaction import TerminalInteraction


def test_messages_queue_fifo_during_turn_and_release_after_normal_finish():
    state = TerminalInteraction()
    state.begin_turn()
    assert state.submit("one") == "queued"
    assert state.submit("two") == "queued"
    assert state.next_message() is None
    assert state.queued_messages() == ("one", "two")
    state.finish_turn(normal=True)
    assert state.next_message() == "one"
    assert state.next_message() == "two"
    assert state.next_message() is None


def test_abort_suspends_queue_but_allows_new_correction():
    state = TerminalInteraction()
    state.begin_turn()
    assert state.submit("queued before abort") == "queued"
    state.finish_turn(normal=False)
    assert state.next_message() is None
    assert state.submit("correct this task") == "send"
    assert state.queued_messages() == ("queued before abort",)
    state.begin_turn()
    state.finish_turn(normal=True)
    assert state.next_message() is None
    state.resume_queue()
    assert state.next_message() == "queued before abort"


def test_queue_clear_and_blank_submit():
    state = TerminalInteraction()
    assert state.submit("  ") == "ignored"
    state.begin_turn()
    state.submit("keep")
    state.suspend_queue()
    assert state.queued_messages() == ("keep",)
    state.clear_queue()
    state.resume_queue()
    state.finish_turn(normal=True)
    assert state.queued_messages() == ()
    assert state.next_message() is None
