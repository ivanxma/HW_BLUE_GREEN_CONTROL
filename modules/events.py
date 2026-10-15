"""Reviewed event plans include definitions and original states; never infer consent."""
import hashlib
import json


def fingerprint(events):
    stable = [{k: v for k, v in event.items() if k != 'last_executed'} for event in events]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def event_key(event):
    return event['event_schema'] + '.' + event['event_name']


def validate_plan(events, review):
    if not review or review.get('fingerprint') != fingerprint(events):
        raise ValueError('Event inventory changed or has not been reviewed; review current events again')
    choices = review.get('choices', [])
    if len(choices) != len(events):
        raise ValueError('Review every scheduled event before execution')
    expected = {(e['event_schema'], e['event_name']) for e in events}
    actual = {(c['event_schema'], c['event_name']) for c in choices}
    if actual != expected:
        raise ValueError('Event selections do not match the current inventory')
    for c in choices:
        if c.get('after') not in {'KEEP', 'ENABLE', 'DISABLE', 'RESTORE'}:
            raise ValueError('Invalid event state selection')
    return choices
