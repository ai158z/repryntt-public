# Repryntt Compute Marketplace Policy

Repryntt compute marketplace settlement is fiat-first.

## Product Boundary

- Providers may sell CPU/GPU compute as a normal paid service.
- Buyers pay through marketplace payment rails such as Stripe Connect.
- Provider payouts happen through the payment processor, not through CR.
- The blockchain is optional proof/audit infrastructure only.
- The chain may record provider announcement hashes, job receipt hashes, and verification hashes.
- The chain must not be used as the payment rail for user compute sales.

## Removed From Repryntt

Repryntt does not ship a CR/SOL exchange, token bridge, or token on-ramp. Any
third party who wants that behavior must build and operate it independently
outside this codebase.

## Avoid

- User compute payouts in chain tokens.
- CR-to-fiat redemption flows.
- Platform custody of buyer funds.
- On-chain escrow for user payments.
- Marketing language such as yield, investment return, or token earnings.

## Recommended Model

Use a centralized marketplace control plane for provider onboarding, job
scheduling, verification, disputes, and reputation. Keep execution distributed
on provider machines. Use a regulated marketplace payments provider for fiat
checkout, connected-account onboarding, refunds, chargebacks, tax forms, and
payouts.
