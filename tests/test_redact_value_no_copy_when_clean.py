"""Redaction: ne pas reconstruire ce qui n'a pas change.

``_redact_value`` reconstruisait integralement chaque dict/liste, soit une
copie profonde de tout le payload a chaque reponse, meme quand rien n'est
redige. Sur une session de 22 Mo, 97,4% des chaines ne contiennent aucun
marqueur sensible: la copie est donc du travail pur perte, et elle tient le
GIL (json/allocations ne le liberent pas), ce qui serialise les onglets.

Contrat verifie ici:
  1. Quand rien n'est redige, la valeur RENVOYEE EST l'objet d'origine
     (identite, pas seulement egalite) et aucun conteneur proportionnel au
     payload n'est alloue en chemin.
  2. Quand quelque chose est redige, un NOUVEL objet est renvoye et
     l'original n'est PAS mute (fail-closed: pas de fuite par aliasing).
  3. Le resultat reste egal a celui de l'ancienne implementation.
"""
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.helpers import _redact_value  # noqa: E402


SECRET = "authorization=Bearer sk-live-abcdef0123456789"


def _reference_redact(v, *, _enabled):
    """Implementation d'origine (copie systematique), pour comparaison."""
    from api.helpers import _redact_text

    if isinstance(v, str):
        return _redact_text(v, _enabled=_enabled)
    if isinstance(v, dict):
        return {k: _reference_redact(x, _enabled=_enabled) for k, x in v.items()}
    if isinstance(v, list):
        return [_reference_redact(x, _enabled=_enabled) for x in v]
    return v


def test_clean_payload_is_returned_by_identity():
    """Rien de sensible -> aucun dict/liste ne doit etre reconstruit."""
    payload = {
        "messages": [
            {"role": "user", "content": "bonjour, ou en est la commande 4512 ?"},
            {"role": "assistant", "content": [{"type": "text", "text": "elle part demain"}]},
        ],
        "meta": {"workspace": "/workspace/project", "count": 3, "ok": True},
    }

    out = _redact_value(payload, _enabled=True)

    assert out is payload, "payload propre reconstruit inutilement"
    assert out["messages"] is payload["messages"]
    assert out["messages"][0] is payload["messages"][0]
    assert out["meta"] is payload["meta"]


def test_clean_payload_does_not_allocate_eager_container_copies():
    """La voie propre doit rester O(1) en allocation de conteneurs temporaires."""
    payload = {
        "dict": {index: index for index in range(20_000)},
        "list": list(range(20_000)),
    }

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        out = _redact_value(payload, _enabled=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    assert out is payload
    # Une liste temporaire de 20 000 pointeurs depasse a elle seule 160 Ko.
    # Le seuil reste volontairement un ordre de grandeur en dessous, sans
    # dependre des details exacts de l'allocateur d'une version de CPython.
    assert peak - before < 16_384, f"copies temporaires detectees: {peak - before} octets"


def test_sensitive_payload_is_copied_and_original_untouched():
    """Quelque chose est redige -> nouvel objet, original intact."""
    inner = {"role": "user", "content": SECRET}
    payload = {"messages": [inner], "meta": {"workspace": "/tmp"}}

    out = _redact_value(payload, _enabled=True)

    # un nouvel objet est renvoye sur le chemin modifie
    assert out is not payload
    assert out["messages"] is not payload["messages"]
    assert out["messages"][0] is not inner

    # l'original n'est pas mute (pas de fuite par aliasing)
    assert inner["content"] == SECRET
    assert payload["messages"][0]["content"] == SECRET

    # le secret est bien masque dans la sortie
    assert out["messages"][0]["content"] != SECRET

    # les branches NON modifiees restent partagees (c'est tout l'interet)
    assert out["meta"] is payload["meta"]


def test_copy_on_first_change_preserves_order_types_and_clean_identity():
    """Une redaction tardive copie seulement son chemin sans reordonner le JSON."""
    before = {"clean": ["avant", 1]}
    after = {"clean": ["apres", 2]}
    payload = {
        "before": before,
        "items": [before, {"secret": SECRET}, after],
        "after": after,
    }

    out = _redact_value(payload, _enabled=True)

    assert type(out) is dict
    assert type(out["items"]) is list
    assert list(out) == list(payload)
    assert out["before"] is before
    assert out["after"] is after
    assert out["items"][0] is before
    assert out["items"][2] is after
    assert out["items"][1] is not payload["items"][1]
    assert out["items"][1]["secret"] != SECRET


def test_matches_reference_implementation():
    """Le resultat doit rester identique a l'implementation d'origine."""
    payload = {
        "messages": [
            {"role": "user", "content": "texte normal"},
            {"role": "assistant", "content": SECRET},
            {"role": "user", "content": ["propre", SECRET, 42, None]},
        ],
        "nested": {"a": {"b": {"c": SECRET}}, "d": {"e": "rien"}},
        "scalars": [1, 2.5, True, None],
    }

    assert _redact_value(payload, _enabled=True) == _reference_redact(payload, _enabled=True)


def test_disabled_redaction_still_shares():
    """Redaction desactivee -> rien ne doit etre copie."""
    payload = {"messages": [{"role": "user", "content": SECRET}]}
    out = _redact_value(payload, _enabled=False)
    assert out is payload


def test_non_container_values_pass_through():
    for v in (42, 2.5, True, None):
        assert _redact_value(v, _enabled=True) is v
