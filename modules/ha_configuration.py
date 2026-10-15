"""Resolve the optional HA configuration consistently for preview and execution."""


def promotion_configuration(params, plan, blue, green):
    candidates = [
        (params.get('ha_configuration_id'), 'Explicit HA configuration override'),
        (plan.get('promoted_ha_configuration_id'), 'Registered promoted HA configuration override'),
        (plan.get('blue_ha_configuration_id'), 'Registered Blue HA configuration'),
        (plan.get('original_configuration_id') if plan.get('original_ha') else None,
         'Original Blue HA configuration'),
        (blue.get('configuration_id') if blue.get('is_highly_available') else None,
         'Current Blue HA configuration'),
        (green.get('configuration_id'), 'Current Green configuration (unchanged)'),
    ]
    for value, source in candidates:
        if value and str(value).strip():
            return {'configuration_id': str(value).strip(), 'source': source}
    raise ValueError('No existing HA configuration could be identified; specify an optional HA configuration override')
