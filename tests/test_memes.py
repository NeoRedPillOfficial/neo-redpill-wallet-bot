from bot import assistant, memes


def test_requests_for_pictures_are_recognised():
    for text in ("make a meme about the bridge", "can you create an image of a robot", "draw me a cartoon cat", "মিম বানাও", "I love memes"):
        assert memes.wants_image(text), text
    for text in ("what is the price of NEO", "how do I make a picture of my QR code", "how do I tip", ""):
        assert not memes.wants_image(text), text


def test_the_idea_is_read_from_the_ai_answer_even_with_extra_words():
    idea = memes.parse_idea('Sure! {"top":"ME","bottom":"WAITING","image":"a snail on a bridge","caption":"lol"} done')
    assert idea == {"top": "ME", "bottom": "WAITING", "image": "a snail on a bridge", "caption": "lol"}
    assert memes.parse_idea('{"refuse": "I can not make that."}') == {"refuse": "I can not make that."}
    assert memes.parse_idea("no json here") is None and memes.parse_idea(None) is None and memes.parse_idea("{}") is None


def test_meme_text_keeps_only_what_the_font_can_draw():
    assert memes.safe_text("WAITING 🌉 for   the bridge") == "WAITING for the bridge"
    assert memes.safe_text("আমি") == ""


def test_a_meme_is_rendered_with_and_without_a_picture():
    from io import BytesIO

    from PIL import Image

    for source in (None, b"not a picture"):  # no picture, or a broken one: a plain card is drawn
        png = memes.render(source, "One does not simply", "check the price once")
        assert Image.open(BytesIO(png)).size == (memes.SIZE, memes.SIZE)
    buf = BytesIO()
    Image.new("RGB", (1400, 900), (40, 120, 200)).save(buf, "PNG")
    assert Image.open(BytesIO(memes.render(buf.getvalue(), "top", "bottom"))).size == (memes.SIZE, memes.SIZE)


def test_long_answers_are_cut_to_fit_a_telegram_message():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(12))
    pieces = assistant.split_text(text, 3300)
    assert len(pieces) > 1 and all(len(p) <= 3300 for p in pieces)
    assert "".join(p.replace(" ", "").replace("\n", "") for p in pieces) == text.replace(" ", "").replace("\n", "")
    assert assistant.split_text("short") == ["short"] and assistant.split_text("") == []
    assert all(len(p) <= 100 for p in assistant.split_text("x" * 450, 100))


def test_greetings_get_a_human_reply_not_the_help_text():
    assert assistant.smalltalk("hi").startswith("Hello!") and "How can I help" in assistant.smalltalk("hello there")
    assert assistant.smalltalk("Assalamu alaikum").startswith("Wa alaikum assalam")
    assert "how can i help" in assistant.smalltalk("I need help").lower()
    assert assistant.smalltalk("how do I tip in a group") is None and assistant.smalltalk("hi, how do I tip?") is None


def test_a_models_thinking_text_is_removed_from_the_answer():
    assert assistant.clean_reply("<think>hmm let me see</think>Hello there!") == "Hello there!"
    assert assistant.clean_reply("<think>never closed") == ""


def test_a_plain_request_for_the_own_balance_is_recognised():
    for text in ("check my balance", "my balance", "balance?", "show my wallet", "how much NEO do I have"):
        assert assistant.wants_own_balance(text), text
    for text in ("how do I check my balance?", "check his balance", "check @bob balance", "price of neo",
                 "what's the balance of 0x1234567890abcdef1234567890abcdef12345678"):
        assert not assistant.wants_own_balance(text), text
