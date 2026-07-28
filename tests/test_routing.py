"""Routing is what turns a single-repo receiver into an org-wide one."""

from __future__ import annotations

import pytest

from err2issue.routing import Router, RoutingError, Rule


def test_exact_match_wins():
    router = Router.from_spec("checkout-api=acme/checkout,billing=acme/billing")
    assert router.resolve("checkout-api") == "acme/checkout"
    assert router.resolve("billing") == "acme/billing"


def test_glob_match():
    router = Router.from_spec("cart-*=acme/cart")
    assert router.resolve("cart-worker") == "acme/cart"
    assert router.resolve("cart-api") == "acme/cart"


def test_exact_beats_glob_regardless_of_declaration_order():
    router = Router.from_spec("cart-*=acme/cart,cart-api=acme/cart-api")
    assert router.resolve("cart-api") == "acme/cart-api"
    assert router.resolve("cart-worker") == "acme/cart"


def test_more_specific_glob_wins():
    router = Router.from_spec("*=acme/catchall,cart-*=acme/cart")
    assert router.resolve("cart-api") == "acme/cart"
    assert router.resolve("unrelated") == "acme/catchall"


def test_default_repo_is_used_when_nothing_matches():
    router = Router.from_spec("cart-*=acme/cart", default_repo="acme/fallback")
    assert router.resolve("payments") == "acme/fallback"


def test_unroutable_without_a_default_returns_none():
    assert Router.from_spec("cart-*=acme/cart").resolve("payments") is None


def test_empty_spec_falls_back_to_the_default():
    assert Router.from_spec("", default_repo="acme/api").resolve("anything") == "acme/api"


def test_empty_service_name_still_resolves_to_the_default():
    """A record with no service.name must not crash the pipeline."""
    router = Router.from_spec("cart-*=acme/cart", default_repo="acme/api")
    assert router.resolve("") == "acme/api"


def test_whitespace_in_the_spec_is_tolerated():
    router = Router.from_spec(" checkout-api = acme/checkout , billing = acme/billing ")
    assert router.resolve("checkout-api") == "acme/checkout"


def test_trailing_comma_is_tolerated():
    assert Router.from_spec("a=acme/a,").resolve("a") == "acme/a"


def test_glob_matching_is_case_sensitive():
    """Service names are identifiers; silently case-folding them hides typos."""
    router = Router.from_spec("Cart-*=acme/cart")
    assert router.resolve("cart-api") is None


@pytest.mark.parametrize("bad", ["nonsense", "svc=", "svc=not-a-repo", "svc=a/b/c", "=acme/x"])
def test_malformed_specs_are_rejected(bad):
    with pytest.raises(RoutingError):
        Router.from_spec(bad)


def test_invalid_default_repo_is_rejected():
    with pytest.raises(RoutingError):
        Router.from_spec("", default_repo="not-a-repo")


def test_specificity_ordering():
    assert Rule("cart-api-*", "a/b").specificity > Rule("cart-*", "a/b").specificity
    assert Rule("*", "a/b").specificity == 0
    assert Rule("exact", "a/b").specificity == len("exact")


def test_describe_exposes_the_effective_table():
    router = Router.from_spec("a=acme/a,b-*=acme/b", default_repo="acme/d")
    described = router.describe()
    assert described["exact"] == {"a": "acme/a"}
    assert described["globs"] == [{"pattern": "b-*", "repo": "acme/b"}]
    assert described["default"] == "acme/d"


def test_repo_names_with_dots_and_dashes_are_valid():
    router = Router.from_spec("svc=my-org/my.repo-name")
    assert router.resolve("svc") == "my-org/my.repo-name"
