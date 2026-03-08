"""
pump.fun bonding curve — direct buy without Jupiter.
Handles tokens that are still on the bonding curve (pre-graduation).
After graduation the buy_pumpfun() returns "GRADUATED" so the caller
can fall back to Jupiter.
"""
from __future__ import annotations

import struct
import base64
import requests

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

# ── pump.fun program constants ────────────────────────────────────────────────

PUMP_PROGRAM_ID     = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
FEE_RECIPIENT       = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgznyZKHP7yL5PgqWFRMhk")
TOKEN_PROGRAM       = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOC_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bfN")
SYSTEM_PROGRAM      = Pubkey.from_string("11111111111111111111111111111111")
RENT_SYSVAR         = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Anchor discriminators (sha256("global:buy")[:8] and sha256("global:sell")[:8])
BUY_DISCRIMINATOR  = bytes([102,  6, 61, 18,   1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1,  127,  131, 173])

# Computed once at module load
_PUMP_GLOBAL,       _ = Pubkey.find_program_address([b"global"],              PUMP_PROGRAM_ID)
_PUMP_EVENT_AUTH,   _ = Pubkey.find_program_address([b"__event_authority"],   PUMP_PROGRAM_ID)


# ── PDA helpers ───────────────────────────────────────────────────────────────

def get_bonding_curve_pda(mint: str) -> str:
    mint_pk = Pubkey.from_string(mint)
    pda, _  = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM_ID)
    return str(pda)


def get_associated_token_address(owner: str, mint: str) -> str:
    owner_pk = Pubkey.from_string(owner)
    mint_pk  = Pubkey.from_string(mint)
    pda, _   = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(TOKEN_PROGRAM), bytes(mint_pk)],
        ASSOC_TOKEN_PROGRAM
    )
    return str(pda)


# ── Bonding curve data ────────────────────────────────────────────────────────

def fetch_bonding_curve_data(mint: str, rpc_url: str) -> dict | None:
    """
    Read the pump.fun bonding curve account on-chain.
    Returns None if not found. Returns dict with `complete=True` if graduated.
    """
    bc_pda = get_bonding_curve_pda(mint)
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "getAccountInfo",
            "params":  [bc_pda, {"encoding": "base64"}],
        }, timeout=10).json()

        value = resp.get("result", {}).get("value")
        if not value:
            return None

        data = base64.b64decode(value["data"][0])
        if len(data) < 49:
            return None

        return {
            "bonding_curve":          bc_pda,
            "virtual_token_reserves": struct.unpack_from("<Q", data,  8)[0],
            "virtual_sol_reserves":   struct.unpack_from("<Q", data, 16)[0],
            "real_token_reserves":    struct.unpack_from("<Q", data, 24)[0],
            "real_sol_reserves":      struct.unpack_from("<Q", data, 32)[0],
            "token_total_supply":     struct.unpack_from("<Q", data, 40)[0],
            "complete":               bool(data[48]),
        }
    except Exception:
        return None


def is_graduated(mint: str, rpc_url: str) -> bool:
    """Return True if bonding curve is complete (graduated to Raydium)."""
    data = fetch_bonding_curve_data(mint, rpc_url)
    if data is None:
        return True   # can't find curve → assume graduated / not a pump token
    return data.get("complete", False)


def is_pumpfun_token(dex_id: str, mint: str, rpc_url: str) -> bool:
    """True if token is still on pump.fun bonding curve."""
    if "pumpfun" in dex_id.lower() or "pump" in dex_id.lower():
        bc = fetch_bonding_curve_data(mint, rpc_url)
        return bc is not None and not bc.get("complete", True)
    return False


# ── Price math ────────────────────────────────────────────────────────────────

def calculate_buy_tokens(sol_lamports: int, bc: dict) -> int:
    """
    Constant product: how many tokens received for sol_lamports input.
    pump.fun takes 1% fee from SOL before applying the curve.
    Uses integer arithmetic throughout to avoid float precision loss on large reserves.
    """
    vtr = bc.get("virtual_token_reserves", 0)
    vsr = bc.get("virtual_sol_reserves", 0)
    if not vtr or not vsr:
        return 0
    sol_net = int(sol_lamports * 0.99)           # 1% fee
    if sol_net <= 0:
        return 0
    # Integer division preserves full precision for large reserve values
    tokens = vtr - (vtr * vsr) // (vsr + sol_net)
    return max(0, tokens)


def price_per_token_usd(bc: dict, sol_price_usd: float) -> float:
    """Spot price of 1 token in USD from bonding curve reserves."""
    vtr = bc["virtual_token_reserves"]
    vsr = bc["virtual_sol_reserves"]
    if not vtr:
        return 0.0
    sol_per_token = vsr / vtr / 1e9   # in SOL (lamports → SOL)
    return sol_per_token * sol_price_usd


# ── ATA existence check ───────────────────────────────────────────────────────

def account_exists(address: str, rpc_url: str) -> bool:
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "getAccountInfo",
            "params":  [address, {"encoding": "base64"}],
        }, timeout=10).json()
        return resp.get("result", {}).get("value") is not None
    except Exception:
        return False


def make_create_ata_idempotent(payer: Pubkey, owner: Pubkey,
                                mint: Pubkey, ata: Pubkey) -> Instruction:
    """create_associated_token_account_idempotent (index 1)."""
    return Instruction(
        program_id=ASSOC_TOKEN_PROGRAM,
        accounts=[
            AccountMeta(payer,          True,  True),
            AccountMeta(ata,            False, True),
            AccountMeta(owner,          False, False),
            AccountMeta(mint,           False, False),
            AccountMeta(SYSTEM_PROGRAM, False, False),
            AccountMeta(TOKEN_PROGRAM,  False, False),
        ],
        data=bytes([1]),   # instruction index 1 = createIdempotent
    )


# ── Recent blockhash ──────────────────────────────────────────────────────────

def get_recent_blockhash(rpc_url: str) -> Hash | None:
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "getLatestBlockhash",
            "params":  [{"commitment": "finalized"}],
        }, timeout=10).json()
        bh = resp["result"]["value"]["blockhash"]
        return Hash.from_string(bh)
    except Exception:
        return None


# ── Buy ───────────────────────────────────────────────────────────────────────

def buy_pumpfun(mint: str, sol_amount: float, keypair: Keypair,
                rpc_url: str, slippage: float = 0.15) -> str:
    """
    Buy a pump.fun bonding curve token directly.

    Returns:
        tx signature (str)  — on success
        "GRADUATED"         — token already on Raydium, use Jupiter instead
        "ERROR: <msg>"      — on failure
    """
    try:
        sol_lamports = int(sol_amount * 1_000_000_000)

        # 1. Fetch bonding curve
        bc = fetch_bonding_curve_data(mint, rpc_url)
        if bc is None:
            return "ERROR: Bonding curve not found — token may not be on pump.fun"
        if bc["complete"]:
            return "GRADUATED"

        # 2. Calculate token amount + slippage
        token_amount = calculate_buy_tokens(sol_lamports, bc)
        if token_amount <= 0:
            return "ERROR: Token amount calculation failed"
        max_sol_cost = int(sol_lamports * (1 + slippage))

        # 3. Derive addresses
        bc_pda      = bc["bonding_curve"]
        assoc_bc    = get_associated_token_address(bc_pda, mint)
        user_str    = str(keypair.pubkey())
        assoc_user  = get_associated_token_address(user_str, mint)

        mint_pk      = Pubkey.from_string(mint)
        bc_pk        = Pubkey.from_string(bc_pda)
        assoc_bc_pk  = Pubkey.from_string(assoc_bc)
        assoc_user_pk = Pubkey.from_string(assoc_user)

        # 4. Blockhash
        recent_bh = get_recent_blockhash(rpc_url)
        if recent_bh is None:
            return "ERROR: Could not fetch blockhash"

        # 5. Build instructions
        instructions = []

        # Create user ATA if it doesn't exist
        if not account_exists(assoc_user, rpc_url):
            instructions.append(make_create_ata_idempotent(
                keypair.pubkey(), keypair.pubkey(), mint_pk, assoc_user_pk
            ))

        # pump.fun buy instruction
        buy_data = BUY_DISCRIMINATOR + struct.pack("<QQ", token_amount, max_sol_cost)
        instructions.append(Instruction(
            program_id=PUMP_PROGRAM_ID,
            accounts=[
                AccountMeta(_PUMP_GLOBAL,           False, False),
                AccountMeta(FEE_RECIPIENT,           False, True),
                AccountMeta(mint_pk,                 False, False),
                AccountMeta(bc_pk,                   False, True),
                AccountMeta(assoc_bc_pk,             False, True),
                AccountMeta(assoc_user_pk,           False, True),
                AccountMeta(keypair.pubkey(),         True,  True),
                AccountMeta(SYSTEM_PROGRAM,          False, False),
                AccountMeta(TOKEN_PROGRAM,           False, False),
                AccountMeta(RENT_SYSVAR,             False, False),
                AccountMeta(_PUMP_EVENT_AUTH,        False, False),
                AccountMeta(PUMP_PROGRAM_ID,         False, False),
            ],
            data=buy_data,
        ))

        # 6. Build + sign transaction
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_bh,
        )
        tx = VersionedTransaction(msg, [keypair])

        # 7. Send
        tx_b64 = base64.b64encode(bytes(tx)).decode()
        send_resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        }, timeout=30).json()

        if "result" in send_resp:
            return send_resp["result"]   # tx signature
        return f"ERROR: {send_resp.get('error', send_resp)}"

    except Exception as e:
        return f"ERROR: {e}"


# ── Sell ──────────────────────────────────────────────────────────────────────

def sell_pumpfun(mint: str, token_amount: int, keypair: Keypair,
                 rpc_url: str, slippage: float = 0.15) -> str:
    """
    Sell pump.fun bonding curve tokens back to SOL.
    token_amount is raw (integer, not UI).
    Returns tx signature or "ERROR: ..."
    """
    try:
        bc = fetch_bonding_curve_data(mint, rpc_url)
        if bc is None:
            return "ERROR: Bonding curve not found"
        if bc["complete"]:
            return "GRADUATED"

        # Estimate SOL out (constant product reverse)
        vtr = bc["virtual_token_reserves"]
        vsr = bc["virtual_sol_reserves"]
        sol_out_raw = int((vsr - int(vtr * vsr / (vtr + token_amount))) * 0.99)  # 1% pump.fun fee
        min_sol_out = int(sol_out_raw * (1 - slippage))

        bc_pda       = bc["bonding_curve"]
        assoc_bc     = get_associated_token_address(bc_pda, mint)
        user_str     = str(keypair.pubkey())
        assoc_user   = get_associated_token_address(user_str, mint)

        mint_pk       = Pubkey.from_string(mint)
        bc_pk         = Pubkey.from_string(bc_pda)
        assoc_bc_pk   = Pubkey.from_string(assoc_bc)
        assoc_user_pk = Pubkey.from_string(assoc_user)

        recent_bh = get_recent_blockhash(rpc_url)
        if recent_bh is None:
            return "ERROR: Could not fetch blockhash"

        sell_data = SELL_DISCRIMINATOR + struct.pack("<QQ", token_amount, min_sol_out)
        instruction = Instruction(
            program_id=PUMP_PROGRAM_ID,
            accounts=[
                AccountMeta(_PUMP_GLOBAL,     False, False),
                AccountMeta(FEE_RECIPIENT,    False, True),
                AccountMeta(mint_pk,          False, False),
                AccountMeta(bc_pk,            False, True),
                AccountMeta(assoc_bc_pk,      False, True),
                AccountMeta(assoc_user_pk,    False, True),
                AccountMeta(keypair.pubkey(),  True,  True),
                AccountMeta(SYSTEM_PROGRAM,   False, False),
                AccountMeta(TOKEN_PROGRAM,    False, False),
                AccountMeta(RENT_SYSVAR,      False, False),
                AccountMeta(_PUMP_EVENT_AUTH, False, False),
                AccountMeta(PUMP_PROGRAM_ID,  False, False),
            ],
            data=sell_data,
        )

        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[instruction],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_bh,
        )
        tx     = VersionedTransaction(msg, [keypair])
        tx_b64 = base64.b64encode(bytes(tx)).decode()

        send_resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        }, timeout=30).json()

        if "result" in send_resp:
            return send_resp["result"]
        return f"ERROR: {send_resp.get('error', send_resp)}"

    except Exception as e:
        return f"ERROR: {e}"
