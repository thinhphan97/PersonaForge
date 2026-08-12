from personaforge_chat.application.text_segmentation import StreamingTextSegmenter


def test_segmenter_emits_complete_clause_before_agent_turn_completes() -> None:
    segmenter = StreamingTextSegmenter()

    assert segmenter.feed("Xin chào bạn. Tôi đang") == ("Xin chào bạn.",)
    assert segmenter.feed(" trả lời câu hỏi") == ()
    assert segmenter.flush() == "Tôi đang trả lời câu hỏi"
    assert segmenter.flush() is None


def test_segmenter_uses_word_boundary_for_long_unpunctuated_text() -> None:
    segmenter = StreamingTextSegmenter(target_characters=20, maximum_characters=30)

    segments = segmenter.feed("một đoạn văn đủ dài để được tách tại khoảng trắng")

    assert segments
    assert all(segment and not segment.startswith(" ") for segment in segments)


def test_segmenter_does_not_split_sentence_on_comma_or_target_length() -> None:
    segmenter = StreamingTextSegmenter(target_characters=20, maximum_characters=160)

    assert segmenter.feed("Xin chào bạn, hôm nay tôi sẽ phân tích hệ thống") == ()
    assert segmenter.feed(" này.") == (
        "Xin chào bạn, hôm nay tôi sẽ phân tích hệ thống này.",
    )
