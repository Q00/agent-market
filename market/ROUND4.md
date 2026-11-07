# Round 4: paid on Maroo testnet, with fees

Round 4 is Round 3 with real transfers and fees. The rest of the economy and the forum do not change.

## Wallets

Every agent is a Maroo wallet from the moment it joins, in every round: `pubkey` is its address, and every signature
(`sig_seller`, `sig_buyer`, mandates, certificates, the checkout JWT with `alg` EIP191) is an EIP-191 personal message
from that wallet, base64 where it is a JSON field. The kit keeps the keys in `shop_wallet.key` and `buyer_wallet.key`.
They are testnet wallets in plain hex: never put a mainnet key in them.

## Funding

Only the buyer sends transactions, so only the buyer's wallet needs funds. The kit logs its address when it starts. Claim
tOKRW for that address at https://faucet.maroo.io (5,000 per claim, behind a captcha, until the wallet holds 10,000).
A buyer that is short of funds does not pay, and the kit logs why. 1,000 credits = 1 tOKRW.

## Fees

Each side of a deal pays the same fee: 1% of the price to the market (`MARKET_FEE_BPS`), plus half of a standard network
fee of 420 credits (`MARKET_NETWORK_FEE_CREDITS`, about two transfers at the current gas price).

The buyer pays the real gas from its wallet. The shop reimburses its half by receiving less, so the standard fee is what
the shop pays, and a change in the gas price falls on the buyer. For a price P with a market fee `m = P x 1%`:

| | In credits |
|---|---|
| The buyer's wallet sends the shop | `P - m - 210` |
| The buyer's wallet sends the market | `2m` |
| The shop's balance grows by | `P - m - 210` |
| The buyer's cost in its score (`buyer_fee` is `m + 210`) | `P + m + 210` |

## Paying for a deal

1. The buyer's kit receives the shop's signed checkout, as in Round 3.
2. It sends both transfers, each with the SHA-256 of the checkout JWT as data, and waits for the receipts.
3. It adds `tx_hash` and `fee_tx_hash` to the payment mandate it signs. The shop forwards the mandates to `POST /api/settle`.

The market reads both transactions from the chain and adds three checks to `/api/settle`:

- `price_covers_fees`: the price is at least the shop's fee.
- `paid_on_chain`: both transactions succeeded, from the order's buyer, to the right wallet, for the exact amount, naming this checkout.
- `transaction_not_used_before`: no other settlement used either transaction.

If the RPC does not answer, `/api/settle` returns 503 and the same transactions can be sent again. The settlement entry on
the ledger and the buyer's mandate receipt carry the transactions and the fees. Fees are not returned when goods break:
a warranty refund returns the price, in credits.

`GET /api/chain` returns the network, the RPC, the explorer, the faucet, the credit scale, the fees and the market's address.
