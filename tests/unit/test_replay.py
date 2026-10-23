from __future__ import annotations

from decimal import Decimal

from wallex_trader.replay import _fill_base_fee, _order_base_fees


def test_order_base_fees_extracts_base_asset_fee() -> None:
    orders = [
        {
            "client_order_id": "argus_x",
            "symbol": "BNBTMN",
            "raw": {
                "fills": [
                    {
                        "price": "204499871",
                        "quantity": "0.00091",
                        "fee": "0.00000273",
                        "feeAsset": "BNB",
                    }
                ]
            },
        }
    ]
    fees = _order_base_fees(orders)
    key = ("argus_x", Decimal("204499871"), Decimal("0.00091"))
    assert fees[key] == Decimal("0.00000273")
    fill = {
        "order_id": "argus_x",
        "price": "204499871.0000000000000000",
        "quantity": "0.0009100000000000",
    }
    assert _fill_base_fee(fill, fees) == Decimal("0.00000273")


def test_order_base_fees_ignores_tmn_fee() -> None:
    orders = [
        {
            "client_order_id": "argus_y",
            "symbol": "BNBTMN",
            "raw": {
                "fills": [
                    {
                        "price": "204400000",
                        "quantity": "0.0009",
                        "fee": "551.88",
                        "feeAsset": "TMN",
                    }
                ]
            },
        }
    ]
    assert _order_base_fees(orders) == {}
