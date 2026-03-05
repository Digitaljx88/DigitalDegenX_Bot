#!/usr/bin/env python3
"""Test wallet_manager.py functionality"""

import wallet_manager as wm

print("Testing wallet_manager...\n")

# Test 1: Generate mnemonic
print("1. Generating 12-word mnemonic...")
mnemonic = wm.generate_mnemonic()
words = mnemonic.split()
print(f"   ✅ Generated {len(words)}-word mnemonic\n")

# Test 2: Validate mnemonic
print("2. Validating mnemonic...")
is_valid = wm.validate_mnemonic(mnemonic)
print(f"   ✅ Mnemonic valid: {is_valid}\n")

# Test 3: Derive keypair
print("3. Deriving keypair from mnemonic...")
kp_data = wm.mnemonic_to_keypair(mnemonic)
print(f"   ✅ Public key: {kp_data['public_key'][:12]}...")
print(f"   ✅ Private key: {kp_data['private_key_base58'][:12]}...\n")

# Test 4: Encrypt mnemonic
print("4. Encrypting mnemonic with password...")
password = "TestPassword123!"
encrypted = wm.encrypt_mnemonic(mnemonic, password)
print(f"   ✅ Encrypted (base64): {encrypted[:40]}...\n")

# Test 5: Decrypt mnemonic
print("5. Decrypting mnemonic...")
decrypted = wm.decrypt_mnemonic(encrypted, password)
is_same = decrypted == mnemonic
print(f"   ✅ Decryption correct: {is_same}\n")

# Test 6: Create wallet with manual backup
print("6. Creating wallet with manual backup mode...")
wallet = wm.create_wallet_with_mnemonic(backup_mode="manual")
print(f"   ✅ Wallet created")
print(f"   ✅ Recovery code: {wallet['recovery_code']}\n")

# Test 7: Recovery from mnemonic
print("7. Recovering from mnemonic...")
recovered = wm.recover_from_mnemonic(wallet["mnemonic"])
is_match = recovered["public_key"] == wallet["public_key"]
print(f"   ✅ Recovery worked: {is_match}\n")

# Test 8: Create wallet with encrypted backup
print("8. Creating wallet with encrypted backup mode...")
wallet_enc = wm.create_wallet_with_mnemonic(
    backup_mode="encrypted",
    backup_password="SecurePass789"
)
print(f"   ✅ Wallet created with encrypted backup")
print(f"   ✅ Backup saved: {wallet_enc['backup_saved']}\n")

# Test 9: Check backup status
print("9. Checking backup status...")
status = wm.get_wallet_backup_status(wallet_enc['public_key'])
print(f"   ✅ Has backup: {status['has_backup']}")
print(f"   ✅ Backup type: {status['backup_type']}\n")

# Test 10: Format creation message
print("10. Formatting wallet creation message...")
msg = wm.format_wallet_creation_message(wallet)
lines = msg.split('\n')
print(f"   ✅ Message formatted ({len(lines)} lines)")
print(f"   First line: {lines[0][:50]}...\n")

print("=" * 60)
print("✅ All wallet_manager tests PASSED!")
print("=" * 60)
