"""
MetaMask (Base network) Momentum Rotation Bot with Strict Stop-Loss
=====================================================================

This bot trades directly on-chain using a wallet's private key (the same
key that backs a MetaMask account) via Uniswap V3 on the Base network.
MetaMask itself has no trading API -- this script IS the "auto-buying bot,"
signing and sending swap transactions on behalf of whatever wallet you
point it at.

Strategy (same logic as the stock version):
  1. Watch a list of ERC-20 tokens on Base, priced in USDC.
  2. Buy into the tokens with the strongest recent momentum.
  3. STOP LOSS: if any held token drops >= STOP_LOSS_PCT (default 1%) from
     its purchase price, sell it back to USDC immediately.
  4. Rotation: USDC freed by a stop-out (or sitting idle) gets redeployed
     into whichever watchlist token currently has the best momentum that
     you don't already hold.

CONTRACT ADDRESSES (Base mainnet, verified against Uniswap's and Circle's
official docs as of this writing -- re-verify at docs.uniswap.org and
developers.circle.com before running with real funds, since addresses
occasionally change or get superseded by newer router versions):
  - Uniswap V3 SwapRouter02:  0x2626664c2603336E57B271c5C0b26F421741e481
  - Uniswap V3 QuoterV2:      0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a
  - USDC (native, Circle):    0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
  - WETH:                     0x4200000000000000000000000000000000000006

SECURITY
--------
- Use a dedicated wallet for this bot. Do not use your main wallet's key.
- The private key is read ONLY from an environment variable, never hardcoded.
- To get a private key from MetaMask: Account menu > Account details >
  Show private key. Treat this like a password -- anyone who has it can
  take every asset in that wallet, instantly and irreversibly.

SETUP
-----
1. Install dependencies:
       pip install web3 --break-system-packages
2. Set your wallet's private key as an environment variable:
       export WALLET_PRIVATE_KEY="0xyourprivatekeyhere"
3. (Optional but recommended) Get a free RPC endpoint from Alchemy or
   Infura for Base mainnet -- the public RPC can be slow/rate-limited:
       export BASE_RPC_URL="https://your-rpc-provider-url"
   If not set, this defaults to the public Base RPC.
4. Edit the CONFIG block below: watchlist tokens, stop-loss %, position size.
5. Fund the wallet with USDC on Base (and a little ETH on Base for gas).
6. Run it:
       python3 metamask_rotation_bot.py
   It starts in DRY_RUN mode -- it will log exactly what it *would* trade
   without sending any transactions. Only set DRY_RUN = False once you've
   watched a few cycles and are comfortable with its decisions.
"""

import os
import json
import time
import logging
from pathlib import Path

from web3 import Web3
from eth_account import Account

# =========================== CONFIG ===========================

DRY_RUN = True  # ALWAYS start here. Only set False when ready to send real transactions.

BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")

# Watchlist: token symbol -> ERC-20 contract address on Base.
# Add/remove tokens here. All are priced and traded against USDC.
WATCHLIST = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
}

USDC_ADDRESS = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
SWAP_ROUTER_ADDRESS = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER_ADDRESS = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")

POOL_FEE_TIER = 500         # 0.05% -- lowest-fee tier with real liquidity for WETH, cbBTC,
                             # and AERO against USDC on Base (verified: each has $190K-$13M+
                             # liquidity at this tier). If you add a token without a liquid
                             # 0.05% pool, check its pools on app.uniswap.org/explore/pools/base
                             # first -- trading against a thin pool causes bad slippage even
                             # with a low fee.
STOP_LOSS_PCT = 0.01        # 1% loss tolerance -- sell immediately if breached
SLIPPAGE_PCT = 0.005        # 0.5% max slippage tolerance on swaps
MOMENTUM_LOOKBACK_SAMPLES = 10  # how many price samples back to compare for momentum
POSITION_SIZE_USDC = 5      # USDC allocated per new position when rotating in
MAX_OPEN_POSITIONS = 2      # cap on how many tokens the bot holds at once
CHECK_INTERVAL_SEC = 60     # how often the bot checks prices/positions
STATE_FILE = Path("bot_state.json")  # local record of entry prices + price history

# =========================== LOGGING ===========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("metamask_rotation_bot.log")],
)
log = logging.getLogger("metamask_rotation_bot")

# =========================== MINIMAL ABIs ===========================

ERC20_ABI = json.loads("""
[
  {"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
  {"constant":true,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
  {"constant":false,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]
""")

QUOTER_ABI = json.loads("""
[
  {"inputs":[{"components":[
      {"internalType":"address","name":"tokenIn","type":"address"},
      {"internalType":"address","name":"tokenOut","type":"address"},
      {"internalType":"uint256","name":"amountIn","type":"uint256"},
      {"internalType":"uint24","name":"fee","type":"uint24"},
      {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}
    ],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],
   "name":"quoteExactInputSingle",
   "outputs":[
      {"internalType":"uint256","name":"amountOut","type":"uint256"},
      {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
      {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
      {"internalType":"uint256","name":"gasEstimate","type":"uint256"}
   ],
   "stateMutability":"nonpayable","type":"function"}
]
""")

SWAP_ROUTER_ABI = json.loads("""
[
  {"inputs":[{"components":[
      {"internalType":"address","name":"tokenIn","type":"address"},
      {"internalType":"address","name":"tokenOut","type":"address"},
      {"internalType":"uint24","name":"fee","type":"uint24"},
      {"internalType":"address","name":"recipient","type":"address"},
      {"internalType":"uint256","name":"amountIn","type":"uint256"},
      {"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},
      {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}
    ],"internalType":"struct ISwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],
   "name":"exactInputSingle",
   "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],
   "stateMutability":"payable","type":"function"}
]
""")


class MetaMaskRotationBot:
    def __init__(self):
        private_key = os.environ.get("WALLET_PRIVATE_KEY")
        if not private_key:
            raise RuntimeError(
                "Missing WALLET_PRIVATE_KEY environment variable. "
                "See the setup instructions at the top of this file."
            )

        self.w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
        if not self.w3.is_connected():
            raise RuntimeError(f"Could not connect to Base RPC at {BASE_RPC_URL}")

        self.account = Account.from_key(private_key)
        self.address = self.account.address

        self.quoter = self.w3.eth.contract(address=QUOTER_ADDRESS, abi=QUOTER_ABI)
        self.router = self.w3.eth.contract(address=SWAP_ROUTER_ADDRESS, abi=SWAP_ROUTER_ABI)
        self.usdc = self.w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        self.usdc_decimals = self.usdc.functions.decimals().call()

        self.tokens = {
            symbol: {
                "address": Web3.to_checksum_address(addr),
                "contract": self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI),
            }
            for symbol, addr in WATCHLIST.items()
        }
        for symbol, t in self.tokens.items():
            t["decimals"] = t["contract"].functions.decimals().call()

        self.state = self._load_state()

        mode = "DRY RUN" if DRY_RUN else "LIVE"
        log.info(f"Bot initialized in {mode} mode. Wallet: {self.address}. Watchlist: {list(WATCHLIST)}")

    # ----------------------- state persistence -----------------------

    def _load_state(self):
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {"positions": {}, "price_history": {s: [] for s in WATCHLIST}}

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    # ----------------------- pricing -----------------------

    def get_price_in_usdc(self, symbol: str) -> float:
        """Quote how much USDC you'd get for 1 unit of the token (spot price)."""
        token = self.tokens[symbol]
        amount_in = 10 ** token["decimals"]  # 1 whole token
        params = {
            "tokenIn": token["address"],
            "tokenOut": USDC_ADDRESS,
            "amountIn": amount_in,
            "fee": POOL_FEE_TIER,
            "sqrtPriceLimitX96": 0,
        }
        try:
            result = self.quoter.functions.quoteExactInputSingle(params).call()
            amount_out = result[0]
            return amount_out / (10 ** self.usdc_decimals)
        except Exception as e:
            log.warning(f"Price quote failed for {symbol}: {e}")
            return None

    def record_price(self, symbol: str, price: float):
        history = self.state["price_history"].setdefault(symbol, [])
        history.append(price)
        if len(history) > MOMENTUM_LOOKBACK_SAMPLES + 5:
            history.pop(0)

    def momentum_score(self, symbol: str):
        history = self.state["price_history"].get(symbol, [])
        if len(history) < 2:
            return None
        window = history[-MOMENTUM_LOOKBACK_SAMPLES:]
        start, end = window[0], window[-1]
        if start == 0:
            return None
        return (end - start) / start

    # ----------------------- balances -----------------------

    def get_usdc_balance(self) -> float:
        raw = self.usdc.functions.balanceOf(self.address).call()
        return raw / (10 ** self.usdc_decimals)

    def get_token_balance(self, symbol: str) -> float:
        token = self.tokens[symbol]
        raw = token["contract"].functions.balanceOf(self.address).call()
        return raw / (10 ** token["decimals"])

    # ----------------------- transaction plumbing -----------------------

    def _send_tx(self, tx):
        tx["nonce"] = self.w3.eth.get_transaction_count(self.address)
        tx["from"] = self.address
        if "gas" not in tx:
            tx["gas"] = self.w3.eth.estimate_gas(tx)
        gas_price = self.w3.eth.gas_price
        tx["maxFeePerGas"] = gas_price * 2
        tx["maxPriorityFeePerGas"] = self.w3.to_wei(0.001, "gwei")
        tx["chainId"] = self.w3.eth.chain_id
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"Transaction sent: {tx_hash.hex()}")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            raise RuntimeError(f"Transaction {tx_hash.hex()} reverted")
        return receipt

    def _ensure_approval(self, token_contract, spender, amount_raw):
        allowance = token_contract.functions.allowance(self.address, spender).call()
        if allowance < amount_raw:
            log.info(f"Approving {spender} to spend token...")
            tx = token_contract.functions.approve(spender, amount_raw).build_transaction({})
            if DRY_RUN:
                log.info("[DRY RUN] Would send approval transaction.")
            else:
                self._send_tx(tx)

    # ----------------------- trading actions -----------------------

    def buy(self, symbol: str, usdc_amount: float):
        token = self.tokens[symbol]
        amount_in_raw = int(usdc_amount * (10 ** self.usdc_decimals))
        price = self.get_price_in_usdc(symbol)
        if price is None:
            log.warning(f"Skipping buy of {symbol}: no price available.")
            return
        expected_out = usdc_amount / price
        min_out_raw = int(expected_out * (1 - SLIPPAGE_PCT) * (10 ** token["decimals"]))

        log.info(f"BUY {symbol}: spend {usdc_amount} USDC, expect ~{expected_out:.6f} {symbol}")

        self._ensure_approval(self.usdc, SWAP_ROUTER_ADDRESS, amount_in_raw)

        params = {
            "tokenIn": USDC_ADDRESS,
            "tokenOut": token["address"],
            "fee": POOL_FEE_TIER,
            "recipient": self.address,
            "amountIn": amount_in_raw,
            "amountOutMinimum": min_out_raw,
            "sqrtPriceLimitX96": 0,
        }
        if DRY_RUN:
            log.info(f"[DRY RUN] Would swap {usdc_amount} USDC -> {symbol}")
        else:
            tx = self.router.functions.exactInputSingle(params).build_transaction({"value": 0})
            self._send_tx(tx)

        self.state["positions"][symbol] = {"entry_price": price, "usdc_spent": usdc_amount}
        self._save_state()

    def sell_all(self, symbol: str):
        token = self.tokens[symbol]
        balance = self.get_token_balance(symbol)
        if balance <= 0:
            log.info(f"No {symbol} balance to sell.")
            self.state["positions"].pop(symbol, None)
            self._save_state()
            return

        amount_in_raw = int(balance * (10 ** token["decimals"]))
        price = self.get_price_in_usdc(symbol)
        expected_out = balance * price if price else 0
        min_out_raw = int(expected_out * (1 - SLIPPAGE_PCT) * (10 ** self.usdc_decimals))

        log.info(f"SELL (full exit) {symbol}: {balance} tokens, expect ~{expected_out:.4f} USDC")

        self._ensure_approval(token["contract"], SWAP_ROUTER_ADDRESS, amount_in_raw)

        params = {
            "tokenIn": token["address"],
            "tokenOut": USDC_ADDRESS,
            "fee": POOL_FEE_TIER,
            "recipient": self.address,
            "amountIn": amount_in_raw,
            "amountOutMinimum": min_out_raw,
            "sqrtPriceLimitX96": 0,
        }
        if DRY_RUN:
            log.info(f"[DRY RUN] Would swap {balance} {symbol} -> USDC")
        else:
            tx = self.router.functions.exactInputSingle(params).build_transaction({"value": 0})
            self._send_tx(tx)

        self.state["positions"].pop(symbol, None)
        self._save_state()

    # ----------------------- core logic -----------------------

    def update_prices(self):
        for symbol in WATCHLIST:
            price = self.get_price_in_usdc(symbol)
            if price is not None:
                self.record_price(symbol, price)
        self._save_state()

    def check_stop_losses(self):
        for symbol, pos in list(self.state["positions"].items()):
            entry_price = pos["entry_price"]
            current_price = self.get_price_in_usdc(symbol)
            if current_price is None or entry_price == 0:
                continue
            change_pct = (current_price - entry_price) / entry_price
            if change_pct <= -STOP_LOSS_PCT:
                log.info(
                    f"STOP LOSS TRIGGERED: {symbol} entry=${entry_price:.6f} "
                    f"current=${current_price:.6f} ({change_pct*100:.2f}%)"
                )
                self.sell_all(symbol)

    def rotate_into_winners(self):
        open_slots = MAX_OPEN_POSITIONS - len(self.state["positions"])
        if open_slots <= 0:
            return

        candidates = []
        for symbol in WATCHLIST:
            if symbol in self.state["positions"]:
                continue
            score = self.momentum_score(symbol)
            if score is not None and score > 0:
                candidates.append((symbol, score))
        candidates.sort(key=lambda x: x[1], reverse=True)

        usdc_balance = self.get_usdc_balance()
        for symbol, score in candidates[:open_slots]:
            if usdc_balance < POSITION_SIZE_USDC:
                log.info("Not enough USDC to open a new position.")
                break
            log.info(f"ROTATE IN: {symbol} (momentum {score*100:.2f}%)")
            self.buy(symbol, POSITION_SIZE_USDC)
            usdc_balance -= POSITION_SIZE_USDC

    def run_once(self):
        log.info("--- cycle start ---")
        self.update_prices()
        self.check_stop_losses()
        self.rotate_into_winners()
        log.info("--- cycle end ---")

    def run_forever(self):
        log.info(f"Starting main loop (checking every {CHECK_INTERVAL_SEC}s). Ctrl+C to stop.")
        while True:
            try:
                self.run_once()
            except Exception as e:
                log.error(f"Unhandled error in cycle: {e}")
            time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    bot = MetaMaskRotationBot()
    bot.run_forever()
