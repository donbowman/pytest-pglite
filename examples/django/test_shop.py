"""Django ORM tests against PGlite."""

from __future__ import annotations

import pytest
from django.db import transaction

from shop.models import Product


@pytest.mark.django_db
def test_create_and_query() -> None:
    Product.objects.create(name="Widget", price="9.99")
    Product.objects.create(name="Gadget", price="19.99")
    assert Product.objects.count() == 2
    widget = Product.objects.get(name="Widget")
    assert str(widget.price) == "9.99"


@pytest.mark.django_db
def test_transactions_are_isolated() -> None:
    # pytest-django wraps each test in a transaction.
    assert Product.objects.count() == 0
    Product.objects.create(name="Kept", price="1.00")
    with pytest.raises(RuntimeError), transaction.atomic():
        Product.objects.create(name="Rolled back", price="2.00")
        raise RuntimeError("force rollback")
    assert Product.objects.count() == 1
