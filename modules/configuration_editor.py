"""HeatWave configuration values, validated independently of SQL SHOW VARIABLES."""
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name('configuration_variables.json').read_text())
VARIABLES = CATALOG['variables']
INITIALIZATION_VARIABLES = {'lower_case_table_names'}
REMOVAL_WARNING = ('Removing a variable from this configuration does not reset or remove an already persisted '
                   'value on an existing DB system. Inherited/default values may still apply. '
                   'Change its value instead when you need to change DB behavior.')


def values(configuration):
    result = {name: ('ON' if value else 'OFF') if isinstance(value, bool) else str(value)
              for name, value in (configuration.get('variables') or {}).items() if value is not None}
    result.update({option['name']: str(option['value']) for option in configuration.get('options') or []
                   if option.get('value') is not None})
    return result


def initialization_values(configuration):
    """Read initialization settings from either OCI representation.

    The modern options list includes initialization settings as well as user
    variables. It is authoritative when the response also has legacy fields.
    """
    result = {name: str(value) for name, value in (configuration.get('init_variables') or {}).items()
              if value is not None}
    result.update({option['name']: str(option['value']) for option in configuration.get('options') or []
                   if option.get('name') in INITIALIZATION_VARIABLES and option.get('value') is not None})
    return result


def next_name(original, configurations):
    base = re.sub(r'_v\d+$', '', original)
    pattern = re.compile(re.escape(base) + r'_v(\d+)$')
    versions = [int(match[1]) for c in configurations + [{'display_name': original}]
                if (match := pattern.fullmatch(c.get('display_name') or ''))]
    name = f'{base}_v{max(versions, default=0) + 1:03d}'
    if len(name) > 255:
        raise ValueError('The original configuration name is too long for a version suffix')
    return name


def validate_values(submitted, original, acknowledge_removal=False):
    if not isinstance(submitted, dict):
        raise ValueError('Configuration variable values are required')
    editable = {name: value for name, value in original.items() if name in VARIABLES}
    removed = sorted(set(editable) - set(submitted))
    required = [name for name in removed if not VARIABLES[name]['removable']]
    if required:
        raise ValueError('HeatWave requires these variables; change their values instead: ' + ', '.join(required))
    if removed and acknowledge_removal is not True:
        raise ValueError(REMOVAL_WARNING)
    result = {}
    for name, value in submitted.items():
        spec = VARIABLES.get(name)
        if not spec:
            raise ValueError('Variable is not an editable HeatWave configuration variable: ' + str(name))
        if not isinstance(value, str) or len(value) > 16384 or '\x00' in value:
            raise ValueError('Invalid value for ' + name)
        # Preserve unchanged service values even if a newer version has different
        # limits from the bundled reference. Changes must pass current validation.
        if value != original.get(name):
            kind = spec['type']
            if kind == 'enum':
                choices = {c.casefold(): c for c in spec['choices']}
                if value.casefold() not in choices:
                    raise ValueError('Choose a supported value for ' + name)
                value = choices[value.casefold()]
            elif kind in {'integer', 'decimal'}:
                if not re.fullmatch(r'-?\d+' if kind == 'integer' else r'-?\d+(?:\.\d+)?', value):
                    raise ValueError('Enter a valid ' + kind + ' for ' + name)
                try:
                    number = Decimal(value)
                    if ('min' in spec and number < Decimal(spec['min'])) or ('max' in spec and number > Decimal(spec['max'])):
                        raise ValueError('Value is outside the HeatWave range for ' + name)
                except InvalidOperation as exc:
                    raise ValueError('Invalid numeric value for ' + name) from exc
            elif kind == 'set':
                selected = [v.strip() for v in value.split(',')] if value else []
                if len(selected) != len(set(selected)) or any(v not in spec['choices'] for v in selected):
                    raise ValueError('Choose supported values for ' + name)
            elif kind == 'flags':
                selected = [v.strip().split('=') for v in value.split(',')] if value else []
                if any(len(v) != 2 or v[0] not in spec['choices'] or v[1] not in {'on', 'off'} for v in selected):
                    raise ValueError('Choose supported flags and on/off values for ' + name)
                if len({v[0] for v in selected}) != len(selected):
                    raise ValueError('Duplicate flag in ' + name)
        result[name] = value
    return result, removed
