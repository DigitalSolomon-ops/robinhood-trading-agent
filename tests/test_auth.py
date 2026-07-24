from __future__ import annotations

from src.auth import RobinhoodAuth


def test_robinhood_signature_matches_official_docs_example() -> None:
    auth = RobinhoodAuth(
        api_key="rh-api-6148effc-c0b1-486c-8940-a1d099456be6",
        private_key_base64="xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU=",
    )
    body = (
        "{'client_order_id': '131de903-5a9c-4260-abc1-28d562a5dcf0', "
        "'side': 'buy', 'symbol': 'BTC-USD', 'type': 'market', "
        "'market_order_config': {'asset_quantity': '0.1'}}"
    )

    headers = auth.headers(
        method="POST",
        path="/api/v1/crypto/trading/orders/",
        body=body,
        timestamp=1698708981,
    )

    assert headers["x-api-key"] == "rh-api-6148effc-c0b1-486c-8940-a1d099456be6"
    assert headers["x-timestamp"] == "1698708981"
    assert headers["x-signature"] == "q/nEtxp/P2Or3hph3KejBqnw5o9qeuQ+hYRnB56FaHbjDsNUY9KhB1asMxohDnzdVFSD7StaTqjSd9U9HvaRAw=="
