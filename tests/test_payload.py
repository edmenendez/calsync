"""Mirror payload builder tests - the privacy field-by-field audit in code."""

import pytest

from calsync.sync.payload import BUSY_COLOR_ID, DEFAULT_FULL_COLOR_MAP, build_mirror_payload


def _src(**overrides) -> dict:
    base = {
        'id': 'src-123',
        'summary': 'Quarterly review with leadership',
        'description': 'Confidential strategy discussion',
        'location': '123 Main St, Conf Room A',
        'start': {'dateTime': '2026-05-04T10:00:00-04:00', 'timeZone': 'America/New_York'},
        'end': {'dateTime': '2026-05-04T11:00:00-04:00', 'timeZone': 'America/New_York'},
        'attendees': [{'email': 'colleague@avela.org', 'self': False}],
        'recurringEventId': 'parent-recurring-id',
        'originalStartTime': {'dateTime': '2026-05-04T10:00:00-04:00'},
        'recurrence': ['RRULE:FREQ=WEEKLY'],
        'conferenceData': {'conferenceId': 'meet-abc-def-ghi'},
        'hangoutLink': 'https://meet.google.com/abc-def-ghi',
        'attachments': [{'fileUrl': 'https://drive.google.com/...'}],
        'source': {'url': 'https://avela.org/events/qbr-2026-q2'},
        'gadget': {'iconLink': 'https://...'},
    }
    base.update(overrides)
    return base


# --- common across modes ---


def test_visibility_is_private():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['visibility'] == 'private'


def test_transparency_is_opaque():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['transparency'] == 'opaque'


def test_reminders_disabled():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['reminders'] == {'useDefault': False, 'overrides': []}


def test_extended_properties_origin_and_mirror_key():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='abc-key',
        source_account_label='avela',
    )
    private = p['extendedProperties']['private']
    assert private == {'calsync_origin': 'calsync', 'calsync_mirror_key': 'abc-key'}


def test_start_and_end_copied_verbatim():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['start'] == {'dateTime': '2026-05-04T10:00:00-04:00', 'timeZone': 'America/New_York'}
    assert p['end'] == {'dateTime': '2026-05-04T11:00:00-04:00', 'timeZone': 'America/New_York'}


def test_missing_start_or_end_raises():
    src = _src()
    del src['start']
    with pytest.raises(ValueError, match='start or end'):
        build_mirror_payload(
            source_event=src,
            mode='busy',
            mirror_key='k',
            source_account_label='avela',
        )


# --- privacy: NEVER copy these fields ---


def test_no_attendees_in_either_mode():
    """Leaks invitee identities."""
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'attendees' not in p


def test_no_recurring_event_id():
    """Leaks recurrence linkage."""
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'recurringEventId' not in p


def test_no_original_start_time():
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'originalStartTime' not in p


def test_no_recurrence_rules():
    """RRULE / RDATE / EXDATE leak schedule structure."""
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'recurrence' not in p


def test_no_attachments():
    """Could embed Drive file IDs / external URLs."""
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'attachments' not in p


def test_no_source_url_backlink():
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'source' not in p


def test_no_gadget_field():
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'gadget' not in p


def test_no_id_or_icaluid_carried_over():
    """Mirror gets a fresh Google-generated id and iCalUID; never echo source's."""
    for mode in ('busy', 'full'):
        p = build_mirror_payload(
            source_event=_src(),
            mode=mode,
            mirror_key='k',
            source_account_label='avela',
        )
        assert 'id' not in p
        assert 'iCalUID' not in p


def test_source_extended_properties_not_echoed():
    """If source has third-party extendedProperties, they MUST NOT travel to the mirror."""
    src = _src()
    src['extendedProperties'] = {'private': {'some_other_app': 'leaks'}, 'shared': {'x': 'y'}}
    p = build_mirror_payload(
        source_event=src,
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['extendedProperties']['private'] == {
        'calsync_origin': 'calsync',
        'calsync_mirror_key': 'k',
    }
    assert 'shared' not in p['extendedProperties']
    assert 'some_other_app' not in p['extendedProperties']['private']


# --- busy mode: title, description, location, conference data stripped ---


def test_busy_mode_summary_is_literally_busy():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['summary'] == 'Busy'


def test_busy_mode_no_description():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert 'description' not in p


def test_busy_mode_no_location():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert 'location' not in p


def test_busy_mode_no_conference_data():
    p = build_mirror_payload(
        source_event=_src(),
        mode='busy',
        mirror_key='k',
        source_account_label='avela',
    )
    assert 'conferenceData' not in p
    assert 'hangoutLink' not in p


def test_busy_mode_uses_graphite_color():
    """All busy mirrors uniform slate-gray regardless of source."""
    for label in ('avela', 'beachmedia', 'novact', 'personal'):
        p = build_mirror_payload(
            source_event=_src(),
            mode='busy',
            mirror_key='k',
            source_account_label=label,
        )
        assert p['colorId'] == BUSY_COLOR_ID == '8'


# --- full mode: details copied, color per source ---


def test_full_mode_copies_summary():
    p = build_mirror_payload(
        source_event=_src(),
        mode='full',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['summary'] == 'Quarterly review with leadership'


def test_full_mode_copies_description_and_location():
    p = build_mirror_payload(
        source_event=_src(),
        mode='full',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['description'] == 'Confidential strategy discussion'
    assert p['location'] == '123 Main St, Conf Room A'


def test_full_mode_copies_conference_data_and_hangout_link():
    p = build_mirror_payload(
        source_event=_src(),
        mode='full',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['conferenceData'] == {'conferenceId': 'meet-abc-def-ghi'}
    assert p['hangoutLink'] == 'https://meet.google.com/abc-def-ghi'


def test_full_mode_omits_optional_fields_when_source_lacks_them():
    src = _src()
    del src['description']
    del src['location']
    del src['conferenceData']
    del src['hangoutLink']
    p = build_mirror_payload(
        source_event=src,
        mode='full',
        mirror_key='k',
        source_account_label='avela',
    )
    assert 'description' not in p
    assert 'location' not in p
    assert 'conferenceData' not in p
    assert 'hangoutLink' not in p


def test_full_mode_per_source_colors():
    expected = {'avela': '3', 'beachmedia': '9', 'novact': '10'}
    assert expected == DEFAULT_FULL_COLOR_MAP
    for label, color_id in expected.items():
        p = build_mirror_payload(
            source_event=_src(),
            mode='full',
            mirror_key='k',
            source_account_label=label,
        )
        assert p['colorId'] == color_id


def test_full_mode_color_map_override():
    p = build_mirror_payload(
        source_event=_src(),
        mode='full',
        mirror_key='k',
        source_account_label='custom',
        color_map={'custom': '11'},
    )
    assert p['colorId'] == '11'


def test_full_mode_omits_color_when_source_label_unknown():
    p = build_mirror_payload(
        source_event=_src(),
        mode='full',
        mirror_key='k',
        source_account_label='unknown_account',
    )
    assert 'colorId' not in p


def test_full_mode_summary_fallback_when_source_missing():
    src = _src()
    del src['summary']
    p = build_mirror_payload(
        source_event=src,
        mode='full',
        mirror_key='k',
        source_account_label='avela',
    )
    assert p['summary'] == '(no title)'


# --- mode validation ---


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match='invalid mode'):
        build_mirror_payload(
            source_event=_src(),
            mode='invalid',
            mirror_key='k',
            source_account_label='avela',
        )
