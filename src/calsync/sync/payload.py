"""Mirror event payload construction.

The single privacy-critical place in the codebase: anything we don't
explicitly opt into here MUST NOT appear on a mirror. The fields below
match the field-by-field privacy audit in the design plan.

Fields NEVER copied to mirrors (privacy):
- recurringEventId, originalStartTime, recurrence (leak recurrence linkage)
- attendees (leak invitee identities)
- attachments (could embed Drive IDs / external URLs)
- source (could embed a backlink revealing the source)
- gadget (legacy; never populate)
- conferenceData, hangoutLink (busy mode only - leaks Meet/Zoom IDs)

Fields ALWAYS set on mirrors:
- visibility = 'private'
- transparency = 'opaque'
- reminders = none
- extendedProperties.private.calsync_origin = 'calsync'
- extendedProperties.private.calsync_mirror_key = <HMAC>
"""

from typing import Literal

# Slate-gray ('Graphite') used for every busy mirror on work calendars
# regardless of source. Recedes visually so real meetings stand out.
BUSY_COLOR_ID = '8'

# Default per-source colors for `full` mode mirrors on the personal aggregator.
# Override via SyncContext.color_map if needed.
DEFAULT_FULL_COLOR_MAP: dict[str, str] = {
    'avela': '3',  # Grape (purple)
    'beachmedia': '9',  # Blueberry (blue)
    'novact': '10',  # Basil (green)
}


def build_mirror_payload(
    *,
    source_event: dict,
    mode: Literal['full', 'busy'],
    mirror_key: str,
    source_account_label: str,
    color_map: dict[str, str] | None = None,
) -> dict:
    """Return the JSON body for events.insert / events.patch on a mirror.

    Caller is responsible for setting start/end formats consistent with
    the source. We copy `start` and `end` verbatim - they are the only
    timing info on a mirror.
    """
    if mode not in ('full', 'busy'):
        raise ValueError(f'invalid mode: {mode!r}')

    payload: dict = {
        'visibility': 'private',
        'transparency': 'opaque',
        'reminders': {'useDefault': False, 'overrides': []},
        'extendedProperties': {
            'private': {
                'calsync_origin': 'calsync',
                'calsync_mirror_key': mirror_key,
            }
        },
    }

    # start/end MUST be present on the source for a mirror to make sense.
    # All-day events (date-only) are filtered out earlier; we copy whatever
    # shape Google sent us so timezone fidelity is preserved.
    if 'start' not in source_event or 'end' not in source_event:
        raise ValueError('source event missing start or end')
    payload['start'] = dict(source_event['start'])
    payload['end'] = dict(source_event['end'])

    if mode == 'busy':
        payload['summary'] = 'Busy'
        payload['colorId'] = BUSY_COLOR_ID
        # description, location, conferenceData, hangoutLink: omitted entirely.
        return payload

    # mode == 'full': personal aggregator gets full details.
    payload['summary'] = source_event.get('summary') or '(no title)'
    if 'description' in source_event:
        payload['description'] = source_event['description']
    if 'location' in source_event:
        payload['location'] = source_event['location']
    if 'conferenceData' in source_event:
        payload['conferenceData'] = source_event['conferenceData']
    if 'hangoutLink' in source_event:
        payload['hangoutLink'] = source_event['hangoutLink']

    colors = color_map or DEFAULT_FULL_COLOR_MAP
    if source_account_label in colors:
        payload['colorId'] = colors[source_account_label]

    return payload
