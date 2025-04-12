

def get_instrument_param(config: dict, instrument_name: str, param_key: str) -> any:
    """
    Retrieve a parameter value from an instrument's config.

    Args:
        config (dict): Parsed YAML configuration.
        instrument_name (str): Name of the instrument (e.g. "49i").
        param_key (str): Parameter key to retrieve (e.g. "id").

    Returns:
        Any: The value of the parameter, or None if not found.
    """
    instruments: list[dict] = config.get("instruments", [])
    for instr in instruments:
        if instr.get("name") == instrument_name:
            return instr.get("params", {}).get(param_key, None)
    return None
