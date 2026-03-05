"""
Secure Wallet Manager — BIP39 Mnemonic + AES-256 Encryption

Features:
  - Generate 12-word BIP39 mnemonic seed phrases
  - Derive Solana keypairs from mnemonics
  - Encrypt mnemonics with AES-256-GCM + password
  - Two backup modes: encrypted storage or manual user copy
  - Recovery from mnemonic phrase
  - Wallet metadata tracking
"""

import json
import os
import time
import base64
import hashlib
import hmac
from typing import Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

try:
    from mnemonic import Mnemonic
except ImportError:
    Mnemonic = None

try:
    from solders.keypair import Keypair
except ImportError:
    Keypair = None

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WALLET_BACKUP_FILE = os.path.join(DATA_DIR, "wallet_backup.json")
os.makedirs(DATA_DIR, exist_ok=True)


def _check_dependencies():
    """Verify required packages are installed."""
    if not Mnemonic:
        raise ImportError("Install: pip install mnemonic")
    if not Keypair:
        raise ImportError("Install: pip install solders")


# ────── Mnemonic Generation ──────────────────────────────────────────────────

def generate_mnemonic(language: str = "english") -> str:
    """Generate a 12-word BIP39 mnemonic."""
    _check_dependencies()
    mnemo = Mnemonic(language)
    return mnemo.generate(strength=128)


def validate_mnemonic(mnemonic: str) -> bool:
    """Check if a mnemonic is valid BIP39."""
    _check_dependencies()
    mnemo = Mnemonic("english")
    return mnemo.check(mnemonic)


# ────── Keypair Derivation ──────────────────────────────────────────────────

def mnemonic_to_keypair(mnemonic: str) -> dict:
    """Derive a Solana keypair from a BIP39 mnemonic."""
    _check_dependencies()
    if not validate_mnemonic(mnemonic):
        raise ValueError("Invalid mnemonic phrase")
    
    mnemo = Mnemonic("english")
    seed = mnemo.to_seed(mnemonic)
    
    try:
        I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
        parse = I[:32]
        
        kp = Keypair.from_secret_key(parse)
        pubkey_str = str(kp.pubkey())
        privkey_b58 = kp.to_base58_string()
        
        return {
            "public_key": pubkey_str,
            "private_key_base58": privkey_b58,
            "path": "m/44'/501'/0'/0'",
        }
    except Exception as e:
        raise RuntimeError(f"Keypair derivation failed: {e}")


# ────── Encryption / Decryption ──────────────────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive AES-256 key from password using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
        backend=default_backend(),
    )
    return kdf.derive(password.encode())


def encrypt_mnemonic(mnemonic: str, password: str) -> str:
    """Encrypt mnemonic with AES-256-GCM. Returns base64(salt || iv || ciphertext)."""
    if not validate_mnemonic(mnemonic):
        raise ValueError("Invalid mnemonic")
    
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(password, salt)
    
    cipher = AESGCM(key)
    mnemonic_bytes = mnemonic.encode()
    ciphertext = cipher.encrypt(iv, mnemonic_bytes, None)
    
    combined = salt + iv + ciphertext
    return base64.b64encode(combined).decode()


def decrypt_mnemonic(encrypted: str, password: str) -> str:
    """Decrypt mnemonic from encrypted string."""
    try:
        combined = base64.b64decode(encrypted)
        salt = combined[:16]
        iv = combined[16:28]
        ciphertext = combined[28:]
        
        key = _derive_key(password, salt)
        cipher = AESGCM(key)
        mnemonic_bytes = cipher.decrypt(iv, ciphertext, None)
        mnemonic = mnemonic_bytes.decode()
        
        if not validate_mnemonic(mnemonic):
            raise ValueError("Decrypted mnemonic is invalid")
        
        return mnemonic
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}")


# ────── Backup Storage ──────────────────────────────────────────────────────

def _load_wallet_backups() -> dict:
    """Load encrypted wallet backups."""
    try:
        with open(WALLET_BACKUP_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"wallets": {}}


def _save_wallet_backups(data: dict) -> None:
    """Save encrypted wallet backups."""
    with open(WALLET_BACKUP_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(WALLET_BACKUP_FILE, 0o600)


def save_mnemonic_encrypted(public_key: str, mnemonic: str, password: str) -> bool:
    """Save encrypted mnemonic backup."""
    if not validate_mnemonic(mnemonic):
        raise ValueError("Invalid mnemonic")
    
    encrypted = encrypt_mnemonic(mnemonic, password)
    backups = _load_wallet_backups()
    wallets = backups.setdefault("wallets", {})
    wallets[public_key] = {
        "encrypted_mnemonic": encrypted,
        "created_ts": int(time.time()),
        "backup_type": "encrypted",
    }
    _save_wallet_backups(backups)
    return True


def get_saved_backup(public_key: str) -> Optional[dict]:
    """Check if wallet has a saved encrypted backup."""
    backups = _load_wallet_backups()
    return backups.get("wallets", {}).get(public_key)


def recover_from_mnemonic(mnemonic: str, password: Optional[str] = None) -> dict:
    """Recover a wallet from a mnemonic phrase."""
    if password:
        try:
            mnemonic = decrypt_mnemonic(mnemonic, password)
        except Exception as e:
            raise ValueError(f"Decryption failed: {e}")
    
    if not validate_mnemonic(mnemonic):
        raise ValueError("Invalid mnemonic phrase")
    
    kp_data = mnemonic_to_keypair(mnemonic)
    kp_data["recovered_ts"] = int(time.time())
    return kp_data


def create_wallet_with_mnemonic(backup_mode: str = "manual", backup_password: Optional[str] = None) -> dict:
    """Full wallet creation with BIP39 mnemonic."""
    _check_dependencies()
    
    mnemonic = generate_mnemonic()
    kp_data = mnemonic_to_keypair(mnemonic)
    public_key = kp_data["public_key"]
    
    result = {
        "mnemonic": mnemonic,
        "public_key": public_key,
        "private_key_base58": kp_data["private_key_base58"],
        "backup_mode": backup_mode,
        "backup_saved": False,
        "recovery_code": hashlib.sha256(mnemonic.encode()).hexdigest()[:8].upper(),
    }
    
    if backup_mode == "encrypted":
        if not backup_password:
            raise ValueError("Password required for encrypted backup")
        save_mnemonic_encrypted(public_key, mnemonic, backup_password)
        result["backup_saved"] = True
    
    return result


def get_wallet_backup_status(public_key: str) -> dict:
    """Check if wallet has a backup."""
    backup = get_saved_backup(public_key)
    if not backup:
        return {"has_backup": False, "backup_type": None, "created_ts": None}
    return {
        "has_backup": True,
        "backup_type": backup.get("backup_type", "unknown"),
        "created_ts": backup.get("created_ts"),
    }


def format_wallet_creation_message(wallet_data: dict) -> str:
    """Format wallet creation for Telegram."""
    mnemonic = wallet_data.get("mnemonic", "")
    pubkey = wallet_data.get("public_key", "")
    backup_mode = wallet_data.get("backup_mode", "")
    recovery_code = wallet_data.get("recovery_code", "")
    
    word_list = mnemonic.split()
    numbered_words = "\n".join(f"{i+1:2}. {word}" for i, word in enumerate(word_list))
    
    msg = (
        f"*✨ New Solana Wallet Created*\n\n"
        f"🔐 *Recovery Code: {recovery_code}*\n"
        f"_(Save this to verify your seed phrase)_\n\n"
        f"📬 *Wallet Address:*\n`{pubkey}`\n\n"
        f"🌱 *12-Word Seed Phrase:*\n"
        f"```\n{numbered_words}\n```\n\n"
        f"⚠️ *CRITICAL SECURITY*\n"
        f"• Write down all 12 words in order\n"
        f"• Store in a safe place (notebook, vault, safe deposit box)\n"
        f"• *Never share* with anyone\n"
        f"• *Never type* into other apps\n"
        f"• This is your *only backup*\n\n"
        f"💾 Backup Mode: *{backup_mode.upper()}*\n"
    )
    
    if backup_mode == "encrypted":
        msg += "_Backup stored encrypted in bot (password protected)_\n"
        msg += "_You still need to remember your password_"
    else:
        msg += "_⚠️ You must manually save these 12 words_\n"
        msg += "_The bot has no copy of this seed_"
    
    return msg


def format_recovery_guide() -> str:
    """Format recovery instructions for users."""
    return (
        "*🌱 Recovering Your Wallet*\n\n"
        "To restore your wallet on another device or after data loss:\n\n"
        "1️⃣ Use `/wallet` → `Recover from Seed`\n"
        "2️⃣ Enter your 12 words in order\n"
        "3️⃣ If encrypted backup, enter your password\n"
        "4️⃣ Your wallet will be restored\n\n"
        "_Your recovery code from creation message validates the seed_"
    )
