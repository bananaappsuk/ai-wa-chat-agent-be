#!/usr/bin/env python3
"""
Idempotent Stripe product/price setup (Starter / Professional / Business).

Usage:
  python -m scripts.setup_stripe_products --mode test --dry-run
  python -m scripts.setup_stripe_products --mode test
  python -m scripts.setup_stripe_products --mode live --confirm-live
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

PLANS = (
    {
        "key": "starter",
        "name": "Starter",
        "description": "AI WhatsApp Agent — Starter plan",
        "unit_amount": 3900,
        "env_test": "STRIPE_TEST_PRICE_STARTER",
        "env_live": "STRIPE_LIVE_PRICE_STARTER",
    },
    {
        "key": "professional",
        "name": "Professional",
        "description": "AI WhatsApp Agent — Professional plan (most popular)",
        "unit_amount": 7900,
        "env_test": "STRIPE_TEST_PRICE_PROFESSIONAL",
        "env_live": "STRIPE_LIVE_PRICE_PROFESSIONAL",
    },
    {
        "key": "business",
        "name": "Business",
        "description": "AI WhatsApp Agent — Business plan",
        "unit_amount": 14900,
        "env_test": "STRIPE_TEST_PRICE_BUSINESS",
        "env_live": "STRIPE_LIVE_PRICE_BUSINESS",
    },
)


def _secret_for_mode(mode: str) -> str:
    if mode == "live":
        return (os.getenv("STRIPE_LIVE_SECRET_KEY") or os.getenv("STRIPE_SECRET_KEY") or "").strip()
    return (os.getenv("STRIPE_TEST_SECRET_KEY") or os.getenv("STRIPE_SECRET_KEY") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/find Stripe products and monthly GBP prices")
    parser.add_argument("--mode", choices=("test", "live"), default="test")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live", action="store_true", help="Required for --mode live")
    args = parser.parse_args()

    if args.mode == "live" and not args.confirm_live:
        print("ERROR: live mode requires --confirm-live", file=sys.stderr)
        return 2

    secret = _secret_for_mode(args.mode)
    if not secret:
        print(f"ERROR: no secret key for mode={args.mode}", file=sys.stderr)
        return 1
    expected_prefix = "sk_live_" if args.mode == "live" else "sk_test_"
    if not secret.startswith(expected_prefix):
        print(
            f"ERROR: secret key prefix does not match mode={args.mode} (expected {expected_prefix})",
            file=sys.stderr,
        )
        return 1

    import stripe

    stripe.api_key = secret
    print(f"Mode={args.mode} dry_run={args.dry_run}")
    print("Creating / reusing Stripe products and monthly GBP prices...\n")

    env_lines: list[str] = []
    for plan in PLANS:
        product = None
        products = stripe.Product.list(limit=100, active=True)
        for p in products.auto_paging_iter():
            meta = p.get("metadata") or {}
            if meta.get("app_plan") == plan["key"]:
                product = p
                break

        if product:
            print(f"  Product exists: {plan['name']} ({product.id})")
        elif args.dry_run:
            print(f"  [dry-run] would create product: {plan['name']}")
            env_key = plan["env_live"] if args.mode == "live" else plan["env_test"]
            env_lines.append(f"{env_key}=price_DRY_RUN_{plan['key']}")
            continue
        else:
            product = stripe.Product.create(
                name=f"AI WhatsApp Agent — {plan['name']}",
                description=plan["description"],
                metadata={"app_plan": plan["key"]},
            )
            print(f"  Created product: {plan['name']} ({product.id})")

        price = None
        prices = stripe.Price.list(product=product.id, active=True, limit=100)
        for pr in prices.auto_paging_iter():
            if (
                pr.get("currency") == "gbp"
                and pr.get("unit_amount") == plan["unit_amount"]
                and (pr.get("recurring") or {}).get("interval") == "month"
            ):
                price = pr
                break

        if price:
            print(f"  Price exists:   £{plan['unit_amount'] / 100:.0f}/mo ({price.id})")
        elif args.dry_run:
            print(f"  [dry-run] would create price £{plan['unit_amount'] / 100:.0f}/mo")
            env_key = plan["env_live"] if args.mode == "live" else plan["env_test"]
            env_lines.append(f"{env_key}=price_DRY_RUN_{plan['key']}")
            continue
        else:
            # Never overwrite existing active prices — only create if missing
            price = stripe.Price.create(
                product=product.id,
                unit_amount=plan["unit_amount"],
                currency="gbp",
                recurring={"interval": "month"},
                metadata={"app_plan": plan["key"]},
            )
            print(f"  Created price:  £{plan['unit_amount'] / 100:.0f}/mo ({price.id})")

        env_key = plan["env_live"] if args.mode == "live" else plan["env_test"]
        env_lines.append(f"{env_key}={price.id}")
        print()

    print("=" * 60)
    print("Environment template (paste into .env — do not commit real IDs to .env.example):\n")
    print(f"STRIPE_MODE={args.mode}")
    for line in env_lines:
        print(line)
    print("=" * 60)
    print("Enterprise is not created in Stripe (Contact Sales only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
