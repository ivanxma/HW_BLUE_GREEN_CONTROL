"""Interval-based GTID comparison; never expand transaction ranges into integers."""
import re

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse(value):
    result = {}
    for entry in (value or '').lower().split(','):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(':')
        if not UUID.fullmatch(parts[0]) or len(parts) < 2:
            raise ValueError('Invalid GTID UUID or interval')
        ranges = result.setdefault(parts[0], [])
        for part in parts[1:]:
            bounds = part.split('-')
            if len(bounds) > 2 or not all(x.isdigit() for x in bounds):
                raise ValueError('Invalid GTID interval')
            lo, hi = int(bounds[0]), int(bounds[-1])
            if lo < 1 or hi < lo:
                raise ValueError('Invalid GTID bounds')
            ranges.append((lo, hi))
    for uid, ranges in result.items():
        merged = []
        for lo, hi in sorted(ranges):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        result[uid] = merged
    return result


def subtract(a, b):
    result = {}
    for uid, ranges in a.items():
        remaining = []
        for lo, hi in ranges:
            cursor = lo
            for blo, bhi in b.get(uid, []):
                if bhi < cursor:
                    continue
                if blo > hi:
                    break
                if blo > cursor:
                    remaining.append((cursor, min(hi, blo - 1)))
                cursor = max(cursor, bhi + 1)
                if cursor > hi:
                    break
            if cursor <= hi:
                remaining.append((cursor, hi))
        if remaining:
            result[uid] = remaining
    return result


def format_set(value):
    return ','.join(uid + ':' + ':'.join(str(lo) if lo == hi else f'{lo}-{hi}' for lo, hi in ranges)
                    for uid, ranges in sorted(value.items()))


def compare(blue, green, green_uuid):
    b, g = parse(blue), parse(green)
    extra, missing = subtract(g, b), subtract(b, g)
    allowed = extra.get(green_uuid.lower(), [])
    unexpected = {k: v for k, v in extra.items() if k != green_uuid.lower()}
    return {'ok': not unexpected, 'exact': not extra and not missing,
            'extra_green': format_set({green_uuid.lower(): allowed}) if allowed else '',
            'unexpected': format_set(unexpected), 'missing_on_green': format_set(missing)}
