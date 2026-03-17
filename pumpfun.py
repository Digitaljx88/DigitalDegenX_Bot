"""
pump.fun bonding curve — direct buy without Jupiter.
Handles tokens that are still on the bonding curve (pre-graduation).
After graduation the buy_pumpfun() returns "GRADUATED" so the caller
can fall back to Jupiter.
"""
from __future__ import annotations

import struct
import base64
import time
import requests

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

import config as _cfg

# ── ComputeBudget program (priority fees) ─────────────────────────────────────

COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")


def _make_set_compute_unit_limit(units: int) -> Instruction:
    """ComputeBudgetInstruction::SetComputeUnitLimit(units: u32)."""
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM,
        accounts=[],
        data=bytes([2]) + struct.pack("<I", units),
    )


def _make_set_compute_unit_price(micro_lamports: int) -> Instruction:
    """ComputeBudgetInstruction::SetComputeUnitPrice(microLamports: u64)."""
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM,
        accounts=[],
        data=bytes([3]) + struct.pack("<Q", micro_lamports),
    )


def _priority_fee_instructions() -> list[Instruction]:
    units  = getattr(_cfg, "PRIORITY_FEE_COMPUTE_UNITS",  200_000)
    price  = getattr(_cfg, "PRIORITY_FEE_MICRO_LAMPORTS", 500_000)
    return [
        _make_set_compute_unit_limit(units),
        _make_set_compute_unit_price(price),
    ]


# ── Blockhash cache ───────────────────────────────────────────────────────────

_bh_cache: dict = {"hash": None, "ts": 0.0}


def _get_cached_blockhash(rpc_url: str) -> Hash | None:
    ttl = getattr(_cfg, "BLOCKHASH_CACHE_TTL_SECS", 20)
    now = time.time()
    if _bh_cache["hash"] and (now - _bh_cache["ts"]) < ttl:
        return _bh_cache["hash"]
    fresh = get_recent_blockhash(rpc_url)
    if fresh:
        _bh_cache["hash"] = fresh
        _bh_cache["ts"]   = now
    return fresh


# ── pump.fun program constants ────────────────────────────────────────────────

PUMP_PROGRAM_ID     = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
FEE_RECIPIENT       = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
TOKEN_PROGRAM       = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM  = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOC_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM      = Pubkey.from_string("11111111111111111111111111111111")
FEE_PROGRAM         = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# Anchor discriminators (sha256("global:buy")[:8] and sha256("global:sell")[:8])
BUY_DISCRIMINATOR  = bytes([102,  6, 61, 18,   1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1,  127,  131, 173])

# Computed once at module load
_PUMP_GLOBAL,       _ = Pubkey.find_program_address([b"global"],              PUMP_PROGRAM_ID)
_PUMP_EVENT_AUTH,   _ = Pubkey.find_program_address([b"__event_authority"],   PUMP_PROGRAM_ID)
_GLOBAL_VOL_ACC,    _ = Pubkey.find_program_address([b"global_volume_accumulator"], PUMP_PROGRAM_ID)

# fee_config PDA: seeds = ["fee_config", <hardcoded 32-byte key>] under FEE_PROGRAM
_FEE_CONFIG_SEED_KEY = bytes([1,86,224,246,147,102,90,207,68,219,21,104,191,23,91,170,
                              81,137,203,151,245,210,255,59,101,93,43,182,253,109,24,176])
_FEE_CONFIG, _       = Pubkey.find_program_address([b"fee_config", _FEE_CONFIG_SEED_KEY], FEE_PROGRAM)


# ── RPC helpers ───────────────────────────────────────────────────────────────

def _read_rpc_candidates(primary_rpc: str) -> list[str]:
    """
    Return deduplicated RPC URLs to use for read-heavy freshness-sensitive calls.
    Primary stays first; backups are read-only helpers.
    """
    urls = [str(primary_rpc or "").strip()]
    for raw in getattr(_cfg, "SOLANA_RPC_BACKUPS", []):
        url = str(raw or "").strip()
        if url:
            urls.append(url)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


# ── PDA helpers ───────────────────────────────────────────────────────────────

def get_bonding_curve_pda(mint: str) -> str:
    mint_pk = Pubkey.from_string(mint)
    pda, _  = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM_ID)
    return str(pda)


def get_mint_token_program(mint: str, rpc_url: str) -> Pubkey:
    """Return the token program that owns this mint (Token or Token-2022)."""
    for candidate_rpc in _read_rpc_candidates(rpc_url):
        try:
            resp = requests.post(candidate_rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getAccountInfo",
                "params":  [mint, {"encoding": "base64"}],
            }, timeout=10).json()
            owner = resp.get("result", {}).get("value", {}).get("owner", "")
            if owner == str(TOKEN_2022_PROGRAM):
                return TOKEN_2022_PROGRAM
            return TOKEN_PROGRAM
        except Exception:
            continue
    return TOKEN_PROGRAM  # default fallback


def get_associated_token_address(owner: str, mint: str,
                                  token_program: Pubkey = None) -> str:
    if token_program is None:
        token_program = TOKEN_PROGRAM
    owner_pk = Pubkey.from_string(owner)
    mint_pk  = Pubkey.from_string(mint)
    pda, _   = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(token_program), bytes(mint_pk)],
        ASSOC_TOKEN_PROGRAM
    )
    return str(pda)


def get_sol_balance(owner: str, rpc_url: str) -> float:
    """Return SOL balance for the given wallet address. Returns 0.0 on any error."""
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [owner],
        }, timeout=10).json()
        lamports = resp.get("result", {}).get("value", 0)
        return lamports / 1_000_000_000
    except Exception:
        return 0.0


def get_token_balance(owner: str, mint: str, rpc_url: str) -> int | None:
    """Return raw token balance for owner's ATA of the given mint.

    Returns:
        int  — confirmed balance (may be 0 if ATA exists but is empty)
        None — RPC/network error (balance unknown)
    """
    try:
        tok_prog = get_mint_token_program(mint, rpc_url)
        ata = get_associated_token_address(owner, mint, tok_prog)
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountBalance",
            "params": [ata],
        }, timeout=10).json()
        if "error" in resp:
            return None
        return int(resp.get("result", {}).get("value", {}).get("amount", "0"))
    except Exception:
        return None


# ── Bonding curve data ────────────────────────────────────────────────────────

def fetch_bonding_curve_data(mint: str, rpc_url: str) -> dict | None:
    """
    Read the pump.fun bonding curve account on-chain.
    Returns None if not found. Returns dict with `complete=True` if graduated.
    """
    bc_pda = get_bonding_curve_pda(mint)
    for candidate_rpc in _read_rpc_candidates(rpc_url):
        try:
            resp = requests.post(candidate_rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getAccountInfo",
                "params":  [bc_pda, {"encoding": "base64"}],
            }, timeout=10).json()

            value = resp.get("result", {}).get("value")
            if not value:
                continue

            data = base64.b64decode(value["data"][0])
            if len(data) < 49:
                continue

            result = {
                "bonding_curve":          bc_pda,
                "virtual_token_reserves": struct.unpack_from("<Q", data,  8)[0],
                "virtual_sol_reserves":   struct.unpack_from("<Q", data, 16)[0],
                "real_token_reserves":    struct.unpack_from("<Q", data, 24)[0],
                "real_sol_reserves":      struct.unpack_from("<Q", data, 32)[0],
                "token_total_supply":     struct.unpack_from("<Q", data, 40)[0],
                "complete":               bool(data[48]),
            }
            # creator pubkey (32 bytes at offset 49)
            if len(data) >= 81:
                result["creator"] = str(Pubkey.from_bytes(data[49:81]))
            # is_mayhem_mode (byte 81), is_cashback_coin (byte 82)
            if len(data) >= 83:
                result["is_mayhem_mode"]  = bool(data[81])
                result["is_cashback_coin"] = bool(data[82])
            return result
        except Exception:
            continue
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
    for candidate_rpc in _read_rpc_candidates(rpc_url):
        try:
            resp = requests.post(candidate_rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getAccountInfo",
                "params":  [address, {"encoding": "base64"}],
            }, timeout=10).json()
            if resp.get("result", {}).get("value") is not None:
                return True
        except Exception:
            continue
    return False


def make_create_ata_idempotent(payer: Pubkey, owner: Pubkey,
                                mint: Pubkey, ata: Pubkey,
                                token_program: Pubkey = None) -> Instruction:
    """create_associated_token_account_idempotent (index 1)."""
    if token_program is None:
        token_program = TOKEN_PROGRAM
    return Instruction(
        program_id=ASSOC_TOKEN_PROGRAM,
        accounts=[
            AccountMeta(payer,          True,  True),
            AccountMeta(ata,            False, True),
            AccountMeta(owner,          False, False),
            AccountMeta(mint,           False, False),
            AccountMeta(SYSTEM_PROGRAM, False, False),
            AccountMeta(token_program,  False, False),
        ],
        data=bytes([1]),   # instruction index 1 = createIdempotent
    )


# ── Recent blockhash ──────────────────────────────────────────────────────────

def get_recent_blockhash(rpc_url: str) -> Hash | None:
    for candidate_rpc in _read_rpc_candidates(rpc_url):
        try:
            resp = requests.post(candidate_rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getLatestBlockhash",
                "params":  [{"commitment": "finalized"}],
            }, timeout=10).json()
            bh = resp["result"]["value"]["blockhash"]
            return Hash.from_string(bh)
        except Exception:
            continue
    return None


# ── Fee recipient (mayhem mode aware) ────────────────────────────────────────

# Cache for the Global account's reserved fee recipients
_mayhem_fee_cache: dict = {"recipients": [], "ts": 0.0}


def _get_fee_recipient(bc: dict, rpc_url: str) -> Pubkey:
    """Return the correct fee recipient. Mayhem tokens need a reserved recipient."""
    if not bc.get("is_mayhem_mode"):
        return FEE_RECIPIENT
    # Fetch reserved fee recipients from the Global account
    now = time.time()
    if _mayhem_fee_cache["recipients"] and (now - _mayhem_fee_cache["ts"]) < 300:
        return _mayhem_fee_cache["recipients"][0]
    try:
        for candidate_rpc in _read_rpc_candidates(rpc_url):
            resp = requests.post(candidate_rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method":  "getAccountInfo",
                "params":  [str(_PUMP_GLOBAL), {"encoding": "base64"}],
            }, timeout=10).json()
            value = resp.get("result", {}).get("value")
            if not value:
                continue
            data = base64.b64decode(value["data"][0])
            # Global account has reserved_fee_recipients after the main fields.
            # The standard fee_recipient is at offset 9 (32 bytes), and
            # reserved_fee_recipients start at offset 73 (after initial_virtual_token_reserves etc.)
            # For now, just try offset 73 for the first reserved recipient
            if len(data) >= 105:
                recip = Pubkey.from_bytes(data[73:105])
                _mayhem_fee_cache["recipients"] = [recip]
                _mayhem_fee_cache["ts"] = now
                return recip
    except Exception:
        pass
    return FEE_RECIPIENT  # fallback


# ── Buy ───────────────────────────────────────────────────────────────────────

def buy_pumpfun(
    mint: str,
    sol_amount: float,
    keypair: Keypair,
    rpc_url: str,
    slippage: float = None,
    *,
    skip_preflight: bool = True,
    max_retries: int = 3,
) -> str:
    """
    Buy a pump.fun bonding curve token directly.

    Returns:
        tx signature (str)  — on success
        "GRADUATED"         — token already on Raydium, use Jupiter instead
        "ERROR: <msg>"      — on failure
    """
    if slippage is None:
        slippage = getattr(_cfg, "PUMPFUN_SLIPPAGE_DEFAULT", 0.05)

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

        # 3. Anti-sandwich: re-fetch bonding curve immediately before building tx.
        # If a sandwich bot bought in between, reserves will have shifted and the
        # price will have moved. Abort if drift exceeds our threshold.
        bc_fresh = fetch_bonding_curve_data(mint, rpc_url)
        if bc_fresh and not bc_fresh.get("complete"):
            tokens_fresh = calculate_buy_tokens(sol_lamports, bc_fresh)
            if token_amount > 0 and tokens_fresh > 0:
                drift = (token_amount - tokens_fresh) / token_amount  # positive = price went up
                max_drift = getattr(_cfg, "ANTI_SANDWICH_PUMPFUN_MAX_DRIFT_PCT", 3.0) / 100
                if drift > max_drift:
                    return (f"ERROR: Price moved {drift * 100:.1f}% since quote "
                            f"(possible front-run) — aborting to protect against sandwich")
            bc = bc_fresh
            token_amount = calculate_buy_tokens(sol_lamports, bc)
            max_sol_cost = int(sol_lamports * (1 + slippage))

        # 4. Detect token program (Token vs Token-2022) and derive addresses
        tok_prog    = get_mint_token_program(mint, rpc_url)
        bc_pda      = bc["bonding_curve"]
        assoc_bc    = get_associated_token_address(bc_pda, mint, tok_prog)
        user_str    = str(keypair.pubkey())
        assoc_user  = get_associated_token_address(user_str, mint, tok_prog)

        mint_pk      = Pubkey.from_string(mint)
        bc_pk        = Pubkey.from_string(bc_pda)
        assoc_bc_pk  = Pubkey.from_string(assoc_bc)
        assoc_user_pk = Pubkey.from_string(assoc_user)

        # 4. Blockhash (cached — avoids a round-trip on every buy)
        recent_bh = _get_cached_blockhash(rpc_url)
        if recent_bh is None:
            return "ERROR: Could not fetch blockhash"

        # 5. Build instructions — priority fees first, then ATA, then buy
        instructions = _priority_fee_instructions()

        # Create user ATA if it doesn't exist
        if not account_exists(assoc_user, rpc_url):
            instructions.append(make_create_ata_idempotent(
                keypair.pubkey(), keypair.pubkey(), mint_pk, assoc_user_pk,
                tok_prog,
            ))

        # Derive new PDAs required by updated pump.fun program
        creator_str = bc.get("creator", "")
        if not creator_str:
            return "ERROR: Could not read creator from bonding curve"
        creator_pk = Pubkey.from_string(creator_str)
        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator_pk)], PUMP_PROGRAM_ID)
        user_vol_acc, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(keypair.pubkey())], PUMP_PROGRAM_ID)
        bc_v2, _ = Pubkey.find_program_address(
            [b"bonding-curve-v2", bytes(mint_pk)], PUMP_PROGRAM_ID)

        # Mayhem mode tokens need the fee recipient from the Global account
        fee_recip = _get_fee_recipient(bc, rpc_url)

        # pump.fun buy instruction (track_volume = Option<bool>::None = Borsh 0x00)
        buy_data = BUY_DISCRIMINATOR + struct.pack("<QQ", token_amount, max_sol_cost) + bytes([0])
        instructions.append(Instruction(
            program_id=PUMP_PROGRAM_ID,
            accounts=[
                AccountMeta(_PUMP_GLOBAL,           False, False),   # 0  global
                AccountMeta(fee_recip,               False, True),   # 1  fee_recipient
                AccountMeta(mint_pk,                 False, False),   # 2  mint
                AccountMeta(bc_pk,                   False, True),   # 3  bonding_curve
                AccountMeta(assoc_bc_pk,             False, True),   # 4  associated_bonding_curve
                AccountMeta(assoc_user_pk,           False, True),   # 5  associated_user
                AccountMeta(keypair.pubkey(),         True,  True),   # 6  user
                AccountMeta(SYSTEM_PROGRAM,          False, False),   # 7  system_program
                AccountMeta(tok_prog,                False, False),   # 8  token_program
                AccountMeta(creator_vault,           False, True),    # 9  creator_vault
                AccountMeta(_PUMP_EVENT_AUTH,        False, False),   # 10 event_authority
                AccountMeta(PUMP_PROGRAM_ID,         False, False),   # 11 program
                AccountMeta(_GLOBAL_VOL_ACC,         False, False),   # 12 global_volume_accumulator
                AccountMeta(user_vol_acc,            False, True),    # 13 user_volume_accumulator
                AccountMeta(_FEE_CONFIG,             False, False),   # 14 fee_config
                AccountMeta(FEE_PROGRAM,             False, False),   # 15 fee_program
                AccountMeta(bc_v2,                   False, True),    # 16 bonding_curve_v2
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

        # 7. Send — callers can disable skipPreflight when reliability matters
        preflight_commitment = "processed" if skip_preflight else "confirmed"
        tx_b64 = base64.b64encode(bytes(tx)).decode()
        send_resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [tx_b64, {
                "encoding":             "base64",
                "skipPreflight":        skip_preflight,
                "preflightCommitment":  preflight_commitment,
                "maxRetries":           max_retries,
            }],
        }, timeout=15).json()

        if "result" in send_resp:
            return send_resp["result"]   # tx signature
        return f"ERROR: {send_resp.get('error', send_resp)}"

    except Exception as e:
        return f"ERROR: {e}"


# ── Sell ──────────────────────────────────────────────────────────────────────

def sell_pumpfun(mint: str, token_amount: int, keypair: Keypair,
                 rpc_url: str, slippage: float = None) -> str:
    """
    Sell pump.fun bonding curve tokens back to SOL.
    token_amount is raw (integer, not UI).
    Returns tx signature or "ERROR: ..."
    """
    if slippage is None:
        slippage = getattr(_cfg, "PUMPFUN_SLIPPAGE_DEFAULT", 0.05)
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

        tok_prog     = get_mint_token_program(mint, rpc_url)
        bc_pda       = bc["bonding_curve"]
        assoc_bc     = get_associated_token_address(bc_pda, mint, tok_prog)
        user_str     = str(keypair.pubkey())
        assoc_user   = get_associated_token_address(user_str, mint, tok_prog)

        mint_pk       = Pubkey.from_string(mint)
        bc_pk         = Pubkey.from_string(bc_pda)
        assoc_bc_pk   = Pubkey.from_string(assoc_bc)
        assoc_user_pk = Pubkey.from_string(assoc_user)

        # Always use fresh blockhash for sells (not cached) to avoid tx expiry
        recent_bh = get_recent_blockhash(rpc_url)
        if recent_bh is None:
            return "ERROR: Could not fetch blockhash"

        # Derive new PDAs
        creator_str = bc.get("creator", "")
        if not creator_str:
            return "ERROR: Could not read creator from bonding curve"
        creator_pk = Pubkey.from_string(creator_str)
        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator_pk)], PUMP_PROGRAM_ID)
        bc_v2, _ = Pubkey.find_program_address(
            [b"bonding-curve-v2", bytes(mint_pk)], PUMP_PROGRAM_ID)

        fee_recip = _get_fee_recipient(bc, rpc_url)

        sell_data = SELL_DISCRIMINATOR + struct.pack("<QQ", token_amount, min_sol_out)
        sell_accounts = [
            AccountMeta(_PUMP_GLOBAL,     False, False),   # 0  global
            AccountMeta(fee_recip,        False, True),    # 1  fee_recipient
            AccountMeta(mint_pk,          False, False),   # 2  mint
            AccountMeta(bc_pk,            False, True),    # 3  bonding_curve
            AccountMeta(assoc_bc_pk,      False, True),    # 4  associated_bonding_curve
            AccountMeta(assoc_user_pk,    False, True),    # 5  associated_user
            AccountMeta(keypair.pubkey(),  True,  True),   # 6  user
            AccountMeta(SYSTEM_PROGRAM,   False, False),   # 7  system_program
            AccountMeta(creator_vault,    False, True),    # 8  creator_vault
            AccountMeta(tok_prog,         False, False),   # 9  token_program
            AccountMeta(_PUMP_EVENT_AUTH, False, False),   # 10 event_authority
            AccountMeta(PUMP_PROGRAM_ID,  False, False),   # 11 program
            AccountMeta(_FEE_CONFIG,      False, False),   # 12 fee_config
            AccountMeta(FEE_PROGRAM,      False, False),   # 13 fee_program
            AccountMeta(bc_v2,            False, True),    # 14 bonding_curve_v2
        ]
        sell_ix = Instruction(
            program_id=PUMP_PROGRAM_ID,
            accounts=sell_accounts,
            data=sell_data,
        )
        instructions = _priority_fee_instructions() + [sell_ix]

        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_bh,
        )
        tx     = VersionedTransaction(msg, [keypair])
        tx_b64 = base64.b64encode(bytes(tx)).decode()

        send_resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [tx_b64, {
                "encoding":            "base64",
                "skipPreflight":       True,
                "preflightCommitment": "processed",
                "maxRetries":          3,
            }],
        }, timeout=15).json()

        if "result" in send_resp:
            return send_resp["result"]
        return f"ERROR: {send_resp.get('error', send_resp)}"

    except Exception as e:
        return f"ERROR: {e}"
