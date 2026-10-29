#!/usr/bin/env python3
"""The model name the client asked for, and the name the bridge answers to.

A client's model list is a *copy* of the bridge's, taken at whatever moment it
was configured. When the curation changes - a provider is narrowed to one
model, an id is renamed - the copy keeps asking for a name LiteLLM no longer
has, and the answer is:

    400 {"/chat/completions: Invalid model name passed in
          model=qianfan-deepseek-v4-flash. Call `/v1/models` to view available
          models for your key."}

That error names the model and tells you to look elsewhere, which is exactly
what a configured client cannot do. The provider is right, the credential is
right, the model is simply the previous spelling of the one that is served.

So the bridge resolves the name the way an operator would: if the requested
name is not served, and exactly one model of that provider *is*, that is the
one being asked for. Ambiguity is left alone - silently answering a different
model is worse than failing - and every remap is logged with both names.
"""

from __future__ import annotations


def normalize(name: str | None) -> str:
    """The comparison form: no ``[1m]`` marker, no case, no surrounding space."""
    if not isinstance(name, str):
        return ""
    return name.split("[", 1)[0].strip().lower()


def provider_prefix(name: str | None) -> str:
    """The provider a model name is grouped under: ``qianfan`` for
    ``qianfan-deepseek-v4-flash``.

    A name with no ``-`` is a bare alias (``main``, ``pro``, ``flash``) and has
    no provider of its own - it must never be remapped onto somebody's model.
    """
    key = normalize(name)
    return key.split("-", 1)[0] if "-" in key else ""


def serves(requested: str, served: set[str]) -> bool:
    """True when the bridge already serves this name (marker and case aside)."""
    if not isinstance(requested, str):
        return False
    key = normalize(requested)
    return any(normalize(name) == key for name in (served or ()))


def remap_model(requested: str, served: set[str]) -> tuple[str, str | None]:
    """Resolve ``requested`` against the names the bridge serves.

    Returns ``(model_to_send, note)``. ``note`` is ``None`` when nothing was
    changed, otherwise a short ``"asked -> served"`` line for the log.
    """
    if not isinstance(requested, str) or not requested.strip() or not served:
        return requested, None
    served_norm = {normalize(name): name for name in served if normalize(name)}
    if normalize(requested) in served_norm:
        return requested, None
    prefix = provider_prefix(requested)
    if not prefix:
        return requested, None
    candidates = {
        spelling
        for key, spelling in served_norm.items()
        if key.split("-", 1)[0] == prefix
    }
    if len(candidates) != 1:
        return requested, None
    target = candidates.pop()
    # The proxy strips a `[1m]` marker before forwarding; sending the bare
    # served spelling keeps the two paths identical.
    return target.split("[", 1)[0], f"{requested} -> {target.split('[', 1)[0]}"


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("requested")
    ap.add_argument("served", nargs="+")
    args = ap.parse_args()
    model, note = remap_model(args.requested, set(args.served))
    print(json.dumps({"model": model, "note": note}, indent=2))
