"""Network-free fixtures shared by offline payment demonstrations and tests."""

MERCHANT = "ABCDEFGH"


class SpyTransport:
    def __init__(self, *, timeout_post=False, order_status="APPROVED", create_status="CREATED"):
        self.calls = []
        self.timeout_post = timeout_post
        self.order_status = order_status
        self.create_status = create_status

    def request(self, method, path, headers, body):
        self.calls.append((method, path, headers.get("PayPal-Request-Id")))
        if method == "POST" and path == "/v1/oauth2/token":
            return 200, {"access_token": "fake_test_token"}
        if method == "POST" and path == "/v2/checkout/orders":
            if self.timeout_post:
                raise TimeoutError("spy timeout")
            return 201, {"id": "ORDER123", "status": self.create_status}
        if method == "GET" and path == "/v2/checkout/orders/ORDER123":
            return 200, {
                "id": "ORDER123",
                "status": self.order_status,
                "purchase_units": [
                    {
                        "amount": {"currency_code": "USD", "value": "1.00"},
                        "payee": {"merchant_id": MERCHANT},
                        "payments": {
                            "captures": [
                                {"id": "CAPTURE123", "status": "COMPLETED"}
                            ]
                        },
                    }
                ],
            }
        if method == "POST" and path == "/v2/checkout/orders/ORDER123/capture":
            return 201, {
                "id": "ORDER123",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {"id": "CAPTURE123", "status": "COMPLETED"}
                            ]
                        }
                    }
                ],
            }
        if method == "GET" and path == "/v2/payments/captures/CAPTURE123":
            return 200, {"id": "CAPTURE123", "status": "COMPLETED"}
        raise AssertionError("unexpected PayPal request")
