"""Grosses chaines: memoiser au lieu de repayer ~15 regex a chaque requete.

``_redact_fn_cached`` excluait du LRU toute chaine > 16 384 caracteres, pour
eviter d'evincer les milliers de petites chaines recurrentes et de gonfler la
RSS. Consequence mesuree sur une session reelle de 22 Mo: 59 grosses chaines
(29 uniques, 0,76 Mo) repassaient l'integralite des regex a CHAQUE requete,
pour un resultat toujours identique — 1,68 s par requete, soit 99,9% du cout
recurrent de la redaction une fois le cache des petites chaines chaud.

Le correctif ajoute un SECOND cache, dedie aux grosses chaines, borne en
nombre d'entrees ET par une taille maximale par entree, pour que la RSS reste
plafonnee. Les chaines geantes (> plafond) restent volontairement non cachees.

Contrat verifie ici:
  1. une grosse chaine est redigee exactement comme avant;
  2. la meme grosse chaine n'est pas recalculee au second appel;
  3. les deux caches restent separes (pas d'eviction croisee);
  4. une chaine au-dela du plafond n'est jamais retenue.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402
from api.helpers import (  # noqa: E402
    _REDACT_CACHE_MAX_TEXT_LEN,
    _REDACT_LARGE_CACHE_MAX_TEXT_LEN,
    _redact_fn_cached,
    _redact_fn_uncached,
)

SECRET = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"


@pytest.fixture(autouse=True)
def _isolate_large_cache():
    """Ne pas laisser ce fichier polluer l'etat global du process.

    Le cache est un singleton de module partage par toute la suite. Le vider
    sans le restaurer laisse les tests suivants demarrer a froid, ce qui
    perturbe ceux qui dependent de fenetres de fraicheur ou de temps de
    reponse. On repart d'un cache vide ET on le revide en sortie, pour que ce
    fichier n'ait aucun effet observable en dehors de lui-meme.
    """
    helpers._redact_fn_large_lru.cache_clear()
    yield
    helpers._redact_fn_large_lru.cache_clear()


def _big(secret: str, size: int) -> str:
    """Chaine > seuil du petit cache, porteuse d'un secret.

    Le remplissage doit etre assez long pour atteindre ``size`` meme au-dela
    du plafond du grand cache, sinon la troncature produit une chaine trop
    courte et le test ne verifie plus ce qu'il annonce.
    """
    unit = "lorem ipsum dolor sit amet "
    filler = unit * (size // len(unit) + 1)
    text = (filler + secret + filler)[:size]
    assert len(text) == size
    return text


def test_large_string_redaction_matches_uncached():
    text = _big(SECRET, _REDACT_CACHE_MAX_TEXT_LEN + 5000)
    assert len(text) > _REDACT_CACHE_MAX_TEXT_LEN
    assert _redact_fn_cached(text) == _redact_fn_uncached(text)
    assert SECRET not in _redact_fn_cached(text)


def test_large_string_is_memoized():
    helpers._redact_fn_large_lru.cache_clear()
    text = _big(SECRET, _REDACT_CACHE_MAX_TEXT_LEN + 7000)

    first = _redact_fn_cached(text)
    misses_after_first = helpers._redact_fn_large_lru.cache_info().misses

    second = _redact_fn_cached(text)
    info = helpers._redact_fn_large_lru.cache_info()

    assert second == first
    assert info.hits >= 1, "grosse chaine recalculee au second appel"
    assert info.misses == misses_after_first, "miss supplementaire inattendu"


def test_small_strings_do_not_use_the_large_cache():
    helpers._redact_fn_large_lru.cache_clear()
    small = f"petit texte {SECRET}"
    assert len(small) <= _REDACT_CACHE_MAX_TEXT_LEN
    _redact_fn_cached(small)
    info = helpers._redact_fn_large_lru.cache_info()
    assert info.hits == 0 and info.misses == 0, "petite chaine routee vers le grand cache"


def test_giant_string_is_not_retained():
    """Au-dela du plafond: correct, mais jamais mis en cache (RSS bornee)."""
    helpers._redact_fn_large_lru.cache_clear()
    giant = _big(SECRET, _REDACT_LARGE_CACHE_MAX_TEXT_LEN + 10000)
    assert len(giant) > _REDACT_LARGE_CACHE_MAX_TEXT_LEN

    out = _redact_fn_cached(giant)

    assert out == _redact_fn_uncached(giant)
    assert SECRET not in out
    info = helpers._redact_fn_large_lru.cache_info()
    assert info.currsize == 0, "chaine geante retenue en cache"


def test_large_cache_is_bounded():
    assert helpers._redact_fn_large_lru.cache_info().maxsize is not None
    assert helpers._redact_fn_large_lru.cache_info().maxsize <= 128
