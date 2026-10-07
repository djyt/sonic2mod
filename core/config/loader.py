"""Reading a config file: YAML with no key given twice, its `variants:` blocks, and the value words
every section shares."""


from typing import Any

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


VARIANTS_KEY = "variants"


def apply_variant(data: dict, variant: str | None, context: str = "config") -> dict:
    """A config's data as `variant` reads it: in every mapping that holds a `variants:` block, the
    block's entry for `variant` is laid over the mapping's own keys (a key given null is removed,
    a list replaces the list) and the block itself is dropped.  No variant: every block dropped,
    the base build.  A variant named by no block is an error that lists the ones there are.

        - primary: DAC
          mix_note: C3
          variants:
            lofi: {mix_note: A2}        # convert.py --variant lofi
    """
    seen: set[str] = set()
    resolved = _resolve(data, variant, seen, context)
    if variant is not None and variant not in seen:
        known = ", ".join(sorted(seen)) or "none"
        raise ValueError(f"{context}: no variant {variant!r} (variants: {known})")
    return resolved


def _resolve(node: Any, variant: str | None, seen: set[str], context: str) -> Any:
    if isinstance(node, list):
        return [_resolve(v, variant, seen, context) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _resolve(v, variant, seen, context) for k, v in node.items() if k != VARIANTS_KEY}
    blocks = node.get(VARIANTS_KEY)
    if blocks is None:
        return out
    if not isinstance(blocks, dict):
        raise ValueError(f"{context}: {VARIANTS_KEY}: must map variant names to the keys they change")
    seen.update(str(name) for name in blocks)
    overlay = blocks.get(variant) if variant is not None else None
    if overlay is None:
        return out
    if not isinstance(overlay, dict):
        raise ValueError(f"{context}: {VARIANTS_KEY}: {variant}: must be a mapping of the keys it changes")
    for k, v in overlay.items():
        if k == VARIANTS_KEY:
            raise ValueError(f"{context}: {VARIANTS_KEY}: {variant}: a variant cannot hold variants")
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _resolve(v, variant, seen, context)
    return out


def read_yaml_file(filepath) -> dict:
    """A config file's mapping (load_yaml), a syntax error named with the file."""
    import yaml
    try:
        with open(filepath) as f:
            return load_yaml(f)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML syntax error in '{filepath}': {e}") from e
