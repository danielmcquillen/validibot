from validibot.core.forms import SupportMessageForm


def test_support_message_form_strips_html_and_normalizes_whitespace():
    form = SupportMessageForm(
        data={
            "subject": "   <strong>Need <em>help</em></strong>   ",
            "message": "Line one\r\nLine two\rLine three\n",
        },
    )

    assert form.is_valid()
    assert form.cleaned_data["subject"] == "Need help"
    assert form.cleaned_data["message"] == "Line one\nLine two\nLine three"


def test_support_message_form_neutralizes_simple_injection_attempt():
    form = SupportMessageForm(
        data={
            "subject": "<script>alert('owned')</script>Support",
            "message": "<img src=x onerror=alert(1)>Need <b>help</b>",
        },
    )

    assert form.is_valid()
    cleaned_subject = form.cleaned_data["subject"]
    cleaned_message = form.cleaned_data["message"]

    assert "<" not in cleaned_subject
    assert ">" not in cleaned_subject
    assert "<" not in cleaned_message
    assert ">" not in cleaned_message
    assert "onerror" not in cleaned_message.lower()


def test_support_message_form_rejects_empty_after_cleaning():
    form = SupportMessageForm(
        data={
            "subject": "<span> </span>",
            "message": "   \n\r\n\t  ",
        },
    )

    assert not form.is_valid()
    assert form.errors["subject"] == ["Please add a little more detail."]
    assert form.errors["message"] == ["Please add a little more detail."]
