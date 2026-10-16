"""Label matching and compact inventory output. No device calls or UI acquisition."""
import json
import unicodedata
from collections import Counter
from difflib import SequenceMatcher


def normalize_label(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def display_labels(apps):
    counts = Counter(normalize_label(a.label) for a in apps)
    return [
        f'{a.label} [{a.native_id or a.id}]' if counts[normalize_label(a.label)] > 1 else a.label
        for a in apps
    ]


def format_apps(inventory):
    apps = sorted(inventory.apps, key=lambda a: (normalize_label(a.label), a.id))
    labels = [label if a.launchable else f'{label} (not launchable)'
              for a, label in zip(apps, display_labels(apps))]
    suffix = '; incomplete' if not inventory.complete else ''
    return f'Apps ({len(apps)}{suffix}): ' + json.dumps(labels, ensure_ascii=False)


class AppMatchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def match_app(label, inventory, *, inventory_call="device.list_apps()"):
    if not inventory.complete:
        raise AppMatchError('APP_INVENTORY_INCOMPLETE', 'App inventory is incomplete; refresh before opening an app')
    apps = sorted((a for a in inventory.apps if a.launchable), key=lambda a: a.id)
    names = display_labels(apps)
    query = normalize_label(label)
    # Exact package IDs and disambiguated display labels remain an escape hatch
    # for identical labels; ordinary discovery does not emit package IDs.
    exact = [a for a, name in zip(apps, names)
             if label.strip().casefold() in {a.id.casefold(), (a.native_id or '').casefold(), name.casefold()}]
    candidates = exact or [a for a in apps if normalize_label(a.label) == query]
    if not candidates and len(query) >= 2:
        candidates = [a for a in apps if normalize_label(a.label).startswith(query)]
        if not candidates:
            candidates = [a for a in apps if query in normalize_label(a.label)]
    if not candidates and len(query) >= 4:
        ranked = sorted(((SequenceMatcher(None, query, normalize_label(a.label), autojunk=False).ratio(), a)
                         for a in apps), key=lambda item: (-item[0], item[1].id))
        if ranked and ranked[0][0] >= .8:
            candidates = [a for score, a in ranked if ranked[0][0] - score < .1]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        labels = display_labels(candidates)
        raise AppMatchError('AMBIGUOUS_TARGET', 'Multiple apps match; use an exact label: ' + json.dumps(labels[:5], ensure_ascii=False))
    raise AppMatchError('TARGET_NOT_FOUND', f'No launchable app matches {label!r}; use await mu.emit(await {inventory_call}) to see app labels')
