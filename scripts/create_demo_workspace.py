#!/usr/bin/env python3
"""Create a disposable, runnable checkout fixture for a real Hermes demo.

Usage: python3 scripts/create_demo_workspace.py /path/to/new/checkout
The destination must be empty. No API key is needed to create or test it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from textwrap import dedent


FILES = {
    "auth/__init__.py": "",
    "auth/tokens.py": '''
        """Synthetic demo tokens; this fixture is not an authentication library."""

        def verify_token(token: str) -> bool:
            """Accept the synthetic demo token."""
            return token == "demo_valid"
    ''',
    "services/__init__.py": "",
    "services/checkout.py": '''
        from auth.tokens import verify_token

        def settle_order(token: str, amount_cents: int) -> dict:
            if not verify_token(token):
                raise PermissionError("invalid token")
            return {"status": "settled", "amount_cents": amount_cents}
    ''',
    "services/receipts.py": '''
        from auth.tokens import verify_token

        def get_receipt(token: str, order_id: str) -> dict:
            if not verify_token(token):
                raise PermissionError("invalid token")
            return {"order_id": order_id, "status": "paid"}
    ''',
    "api/__init__.py": "",
    "api/gateway.py": '''
        from services.checkout import settle_order
        from services.receipts import get_receipt

        def post_checkout(token: str, amount_cents: int) -> dict:
            return settle_order(token, amount_cents)

        def receipt_status(token: str, order_id: str) -> dict:
            return get_receipt(token, order_id)
    ''',
    "tests/test_checkout.py": '''
        import unittest
        from api.gateway import post_checkout, receipt_status

        class CheckoutTests(unittest.TestCase):
            def test_valid_checkout(self):
                self.assertEqual(post_checkout("demo_valid", 4900),
                                 {"status": "settled", "amount_cents": 4900})

            def test_receipt(self):
                self.assertEqual(receipt_status("demo_valid", "order_42"),
                                 {"order_id": "order_42", "status": "paid"})

            def test_invalid_token_rejected(self):
                with self.assertRaises(PermissionError):
                    post_checkout("invalid", 4900)
                with self.assertRaises(PermissionError):
                    receipt_status("invalid", "order_42")
    ''',
}


def create_workspace(destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for rel_path, source in FILES.items():
        target = destination / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(source).lstrip(), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    create_workspace(args.destination.resolve())
    print(f"Created demo workspace: {args.destination.resolve()}")
    print("Baseline: python3 -m unittest discover -s tests -v")
    print("Hermes prompt: Inspect @impact:auth/tokens.py, then make verify_token")
    print("require an audience argument and accept only audience='checkout'.")
    print("Update affected callers and run the tests.")


if __name__ == "__main__":
    main()
