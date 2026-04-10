"""Apprise plugin catalog introspection and URL assembly."""

import copy
import re
from functools import lru_cache
from urllib.parse import quote

import apprise

from src.core.logging import get_logger

logger = get_logger(__name__)


def _get_scheme(plugin_entry: dict) -> str | None:
    """Return the primary URL scheme for a plugin entry."""
    for key in ("secure_protocols", "protocols"):
        protos = plugin_entry.get(key)
        if protos:
            return protos[0]
    return None


@lru_cache(maxsize=1)
def _build_catalog() -> dict[str, dict]:
    """Build a dict mapping scheme → plugin entry. Cached after first call."""
    details = apprise.Apprise().details()
    catalog: dict[str, dict] = {}
    for entry in details.get("schemas", []):
        scheme = _get_scheme(entry)
        if scheme and scheme not in catalog:
            catalog[scheme] = entry
    return catalog


def _extract_path_tokens(template: str) -> set[str]:
    """Return token names referenced in a URL template, excluding 'schema'."""
    return set(re.findall(r"\{(\w+)\}", template)) - {"schema"}


def _group_templates(entry: dict) -> dict[frozenset, list[int]]:
    """
    Group template indices by the set of required path tokens each uses.

    Returns a mapping from frozenset(required_path_tokens) → list[template_index].

    Variant order is determined by the order templates appear in
    apprise.Apprise().details()["schemas"], which is stable within an Apprise
    version but may change between versions.
    """
    tokens = entry["details"]["tokens"]
    required_set = {k for k, v in tokens.items() if v.get("required") and k != "schema"}
    templates = entry["details"]["templates"]
    groups: dict[frozenset, list[int]] = {}
    for i, template in enumerate(templates):
        path_tokens = _extract_path_tokens(template)
        used_required = frozenset(path_tokens & required_set)
        groups.setdefault(used_required, []).append(i)
    return groups


def _detect_variants(plugin_entry: dict) -> list[dict]:
    """
    Group templates by their required-token set.
    Returns [] if only one group (no variant selector needed).
    """
    groups = _group_templates(plugin_entry)

    if len(groups) <= 1:
        return []

    variants = []
    service_name = str(plugin_entry["service_name"])
    for required_names, _indices in groups.items():
        if required_names:
            label = f"{service_name} ({', '.join(sorted(required_names))})"
        else:
            label = f"{service_name} (no required tokens)"
        variants.append(
            {
                "label": label,
                "required_token_names": sorted(required_names),
            }
        )
    return variants


def _build_token_meta(tokens_dict: dict) -> dict[str, dict]:
    """Convert raw Apprise token defs to clean dicts, excluding the 'schema' token."""
    result = {}
    for name, raw in tokens_dict.items():
        if name == "schema" or "alias_of" in raw:
            continue
        values = raw.get("values")
        if values and not isinstance(values, list):
            # frozenset or other non-list — convert
            try:
                values = sorted(values)
            except TypeError:
                values = list(values)
        regex_val = raw.get("regex")
        regex_str = regex_val[0] if isinstance(regex_val, (list, tuple)) else regex_val
        result[name] = {
            "name": raw.get("name", name),
            "type": raw.get("type", "string"),
            "required": bool(raw.get("required", False)),
            "private": bool(raw.get("private", False)),
            "default": raw.get("default"),
            "values": values if isinstance(values, list) else None,
            "regex": regex_str,
        }
    return result


@lru_cache(maxsize=1)
def _list_plugins_cached() -> tuple[dict, ...]:
    """Build and cache the sorted plugin list as an immutable tuple."""
    catalog = _build_catalog()
    items = [
        {
            "plugin_schema": scheme,
            "service_name": str(entry["service_name"]),
            "setup_url": entry.get("setup_url"),
            "service_url": entry.get("service_url"),
            "category": entry.get("category"),
        }
        for scheme, entry in catalog.items()
    ]
    return tuple(sorted(items, key=lambda x: x["service_name"].lower()))


def list_plugins() -> list[dict]:
    """Return sorted list of plugin dicts for all plugins.

    Keys: plugin_schema, service_name, setup_url, service_url, category.
    """
    return list(copy.deepcopy(_list_plugins_cached()))


def get_service_name(schema: str) -> str:
    """Return human-readable service name for a plugin schema, or the schema itself."""
    for p in _list_plugins_cached():
        if p["plugin_schema"] == schema:
            return p["service_name"]
    return schema


def get_plugin_detail(schema: str) -> dict | None:
    """
    Return token defs and variant info for a plugin, or None if unknown.

    Returns: {plugin_schema, service_name, setup_url, service_url,
              tokens: {name: TokenMeta}, variants: [...]}
    """
    catalog = _build_catalog()
    entry = catalog.get(schema.lower())
    if not entry:
        return None
    tokens = _build_token_meta(entry["details"]["tokens"])
    variants = _detect_variants(entry)
    return {
        "plugin_schema": schema.lower(),
        "service_name": str(entry["service_name"]),
        "setup_url": entry.get("setup_url"),
        "service_url": entry.get("service_url"),
        "tokens": tokens,
        "variants": variants,
    }


def assemble_url(
    schema: str,
    tokens: dict[str, str],
    variant_index: int | None = None,
) -> str:
    """
    Assemble a valid Apprise URL from a plugin schema and token values.

    variant_index is a positional index into the list returned by
    get_plugin_detail(schema)["variants"]. Out-of-range values fall back to
    trying all templates.

    Raises ValueError if schema is unknown, required tokens are missing,
    or the assembled URL fails Apprise validation.
    """
    catalog = _build_catalog()
    entry = catalog.get(schema.lower())
    if not entry:
        raise ValueError(f"Unknown Apprise plugin schema: {schema!r}")

    scheme = _get_scheme(entry)
    all_tokens = entry["details"]["tokens"]
    required_set = {k for k, v in all_tokens.items() if v.get("required") and k != "schema"}
    templates = list(entry["details"]["templates"])

    # Build map_to mapping: token name → URL placeholder name.
    # e.g. Mailgun's target_email has map_to="targets", so submitting
    # {"target_email": "x"} should substitute into {targets} in the template.
    map_to: dict[str, str] = {}
    for tok_name, tok_def in all_tokens.items():
        dest = tok_def.get("map_to")
        if dest and dest != tok_name:
            map_to[tok_name] = dest

    # Remap submitted token keys using map_to, preserving any direct-match keys.
    remapped_tokens: dict[str, str] = {}
    for name, value in tokens.items():
        dest = map_to.get(name, name)
        # Direct key wins over remapped key if both provided
        if dest not in tokens:
            remapped_tokens[dest] = value
        else:
            remapped_tokens[name] = value
    tokens = remapped_tokens

    # Filter to variant templates if requested.
    # variant_index is a positional index into the list returned by
    # get_plugin_detail(schema)["variants"]; out-of-range values are ignored
    # and fall back to trying all templates.
    if variant_index is not None:
        variants = _detect_variants(entry)
        if variants and 0 <= variant_index < len(variants):
            groups = _group_templates(entry)
            variant_indices = list(groups.values())[variant_index]
            templates = [templates[i] for i in variant_indices]

    # Prefer templates that use more of the provided tokens (e.g. include
    # {targets} when a target was provided) before falling back to shorter ones.
    provided_keys = set(tokens.keys())
    templates = sorted(
        templates,
        key=lambda t: len(_extract_path_tokens(t) & provided_keys),
        reverse=True,
    )

    for template in templates:
        path_tokens = _extract_path_tokens(template)
        needed_required = path_tokens & required_set
        if not needed_required.issubset(tokens.keys()):
            continue
        # Apprise templates for some plugins (Mailgun, SparkPost, SMTP2Go) use
        # {host}:{apikey} which places the key in the URL port position. Python's
        # URL parser drops non-numeric ports, so Apprise reads apikey as None.
        # Normalise: replace {host}:{non_port_token} with {host}/{non_port_token}.
        normalised = re.sub(
            r"(\{(?:host|domain|hostname)\}):\{(?!port\})(\w+)\}",
            r"\1/{\2}",
            template,
        )
        # Substitute schema value
        url = normalised.replace("{schema}", scheme or schema)
        # Substitute provided tokens; percent-encode values placed in URL path
        # segments so that characters like @ in email addresses don't corrupt
        # the URL structure. Values in the user/password positions (before @)
        # use the standard URL auth encoding (safe chars include most punctuation).
        for name, value in tokens.items():
            placeholder = "{" + name + "}"
            if placeholder in url:
                # Determine position: before the last @ in netloc = auth, else path
                pos = url.find(placeholder)
                at_in_netloc = url.rfind("@", url.find("://") + 3)
                if at_in_netloc != -1 and pos < at_in_netloc:
                    # Auth position — encode only truly unsafe chars
                    encoded = quote(str(value), safe="!$&'()*+,;=-._~")
                else:
                    # Path position — encode @ and other delimiters
                    encoded = quote(str(value), safe="-._~!$&'()*+,;=")
                url = url.replace(placeholder, encoded)
        # Strip remaining unfilled optional tokens (path segments)
        url = re.sub(r"/\{[^}]+\}", "", url)
        url = re.sub(r"\{[^}]+\}", "", url)
        # Validate
        ap = apprise.Apprise()
        if ap.add(url):
            return url
        logger.debug("assembled url failed apprise validation", extra={"url": url})

    # Report which required tokens are missing
    provided = set(tokens.keys())
    missing = required_set - provided
    if missing:
        raise ValueError(f"Missing required tokens for {schema!r}: {sorted(missing)}")
    raise ValueError(f"Could not assemble a valid Apprise URL for schema {schema!r}")
