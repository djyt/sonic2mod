"""Reading a config file: YAML with no key given twice, and the value words every section shares."""


from ..audio import DITHER_MODES


def mode_word(v) -> str:
    """A mode setting's value as written: YAML 1.1 reads a bare `off` as false (and `on` as true)."""
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v).lower()


def dither_mode(v, context: str) -> str:
    """A `dither` value: one of core.audio.pcm.DITHER_MODES."""
    mode = mode_word(v)
    if mode not in DITHER_MODES:
        raise ValueError(f"{context}: dither must be one of {', '.join(DITHER_MODES)} (got {v!r})")
    return mode


def load_yaml(stream):
    """yaml.safe_load that refuses a mapping with a key given twice.

    PyYAML keeps the last value silently, so a merge group written without its leading `- `
    ("primary: FM5" under the group above) rewrote that group's primary and column and the
    chords came out as an FM5 mix on the arp column; now it is an error at the line.
    """
    import yaml

    class _Strict(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f"line {key_node.start_mark.line + 1}: key {key!r} is given twice in one mapping "
                                 f"(a list item missing its leading '- ' merges into the item above)")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)
    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    return yaml.load(stream, Loader=_Strict)


def read_yaml_file(filepath) -> dict:
    """A config file's mapping (load_yaml), a syntax error named with the file."""
    import yaml
    try:
        with open(filepath) as f:
            return load_yaml(f)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML syntax error in '{filepath}': {e}") from e
