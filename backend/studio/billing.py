from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import Ledger, Wallet


@transaction.atomic
def post(user_id, kind, amount, key):
    wallet, _ = Wallet.objects.get_or_create(user_id=user_id)
    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
    if Ledger.objects.filter(key=key).exists():
        return wallet
    if amount < 0:
        raise ValueError("Negative credit amount")
    if kind == "grant":
        wallet.available += amount
    elif kind == "reserve":
        if wallet.available < amount:
            raise ValidationError("Not enough credits.")
        wallet.available -= amount
        wallet.held += amount
    elif kind in ("settle", "release"):
        if wallet.held < amount:
            raise ValueError("Reservation missing")
        wallet.held -= amount
        if kind == "settle":
            wallet.spent += amount
        else:
            wallet.available += amount
    else:
        raise ValueError("Unknown ledger operation")
    wallet.save()
    Ledger.objects.create(wallet=wallet, key=key, kind=kind, amount=amount)
    return wallet
