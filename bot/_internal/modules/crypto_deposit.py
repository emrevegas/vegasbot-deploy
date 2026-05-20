"""Crypto Deposit Engine — SOL & LTC HD wallet + auto-detection.

Master mnemonic: set CRYPTO_MNEMONIC in .env (BIP39 12/24-word phrase).
  Generate one at: https://iancoleman.io/bip39/

Wallet data:  server/crypto_wallets   → {user_id: {sol:{address,last_balance},ltc:{...},index,check_until}}
Wallet index: server/crypto_wallet_index → {next_index: int}
Settings:     server/crypto_settings  → {enabled, sol_enabled, ltc_enabled, min_deposit_usd}

Monitoring is active for a user for 24 h after they last viewed their deposit address.
BlockCypher free tier: 200 req/hr.  Max ~60 users can be monitored per 2-min cycle.
"""

import os
import time
import requests

from modules.database import get_data, replace_data

MNEMONIC          = os.getenv("CRYPTO_MNEMONIC", "")    # HD seed for USER deposit wallets
TREASURY_MNEMONIC = os.getenv("TREASURY_MNEMONIC", "")  # Separate seed for the main/treasury wallet
BLOCKCYPHER_TOKEN = os.getenv("BLOCKCYPHER_TOKEN", "")  # optional — raises free-tier rate limit
SOL_RPC           = "https://api.mainnet-beta.solana.com"
BLOCKCYPHER_BASE  = "https://api.blockcypher.com/v1/ltc/main"  # primary LTC provider
LTC_API           = BLOCKCYPHER_BASE + "/addrs"               # balance endpoint
LTC_ESPLORA       = "https://litecoinspace.org/api"            # UTXOs + broadcast
COINGECKO         = "https://api.coingecko.com/api/v3/simple/price"
MONITOR_TTL       = 86_400   # 24 h window per user
BATCH_LIMIT       = 60       # max addresses checked per 5-min cycle
SOL_FEE_LAMPORTS  = 5_000    # ~0.000005 SOL fee
LTC_FEE_SATOSHIS  = 10_000   # ~0.0001 LTC fee

_rate_cache: dict = {}
_RATE_TTL         = 60   # seconds

# Balance cache — populated by ltc_prefetch_balances(); valid for one monitor cycle.
_ltc_cache: dict[str, tuple[int, float]] = {}  # address → (satoshis, timestamp)
_LTC_CACHE_TTL = 280  # seconds — slightly less than the 5-min monitor cycle


# ── Seed / address derivation ──────────────────────────────────────────────────

def _seed() -> bytes:
    """BIP39 seed for USER deposit HD wallets (CRYPTO_MNEMONIC)."""
    from bip_utils import Bip39SeedGenerator
    if not MNEMONIC:
        raise RuntimeError("CRYPTO_MNEMONIC not set in .env")
    return Bip39SeedGenerator(MNEMONIC).Generate()


def _treasury_seed() -> bytes:
    """BIP39 seed for the TREASURY/main wallet (TREASURY_MNEMONIC)."""
    from bip_utils import Bip39SeedGenerator
    if not TREASURY_MNEMONIC:
        raise RuntimeError("TREASURY_MNEMONIC not set in .env")
    return Bip39SeedGenerator(TREASURY_MNEMONIC).Generate()


def derive_sol_address(index: int) -> str:
    from bip_utils import Bip44, Bip44Coins, Bip44Changes
    ctx = (
        Bip44.FromSeed(_seed(), Bip44Coins.SOLANA)
        .Purpose().Coin().Account(index)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    )
    return ctx.PublicKey().ToAddress()


def derive_ltc_address(index: int) -> str:
    from bip_utils import Bip44, Bip44Coins, Bip44Changes
    ctx = (
        Bip44.FromSeed(_seed(), Bip44Coins.LITECOIN)
        .Purpose().Coin().Account(index)
        .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
    )
    return ctx.PublicKey().ToAddress()


# ── Wallet store ───────────────────────────────────────────────────────────────

def _all_wallets() -> dict:
    return get_data("server/crypto_wallets") or {}


def _save_wallets(wallets: dict) -> None:
    replace_data("server/crypto_wallets", wallets)


def get_or_create_addresses(user_id: int) -> dict:
    """Return the user's wallet dict, creating HD addresses on first call."""
    uid     = str(user_id)
    wallets = _all_wallets()

    if uid in wallets:
        # Refresh the monitoring window
        wallets[uid]["check_until"] = int(time.time()) + MONITOR_TTL
        _save_wallets(wallets)
        return wallets[uid]

    settings = get_settings()
    idx_data = get_data("server/crypto_wallet_index") or {}
    index    = int(idx_data.get("next_index", 0))

    wallet: dict = {"index": index, "check_until": int(time.time()) + MONITOR_TTL}

    if settings.get("sol_enabled", True):
        wallet["sol"] = {"address": derive_sol_address(index), "last_balance": -1}
    if settings.get("ltc_enabled", True):
        wallet["ltc"] = {"address": derive_ltc_address(index), "last_balance": -1}

    wallets[uid] = wallet
    _save_wallets(wallets)
    replace_data("server/crypto_wallet_index", {"next_index": index + 1})
    return wallet


def get_active_user_ids() -> list[str]:
    """Return user IDs that are within their 24-hour monitoring window."""
    now     = int(time.time())
    wallets = _all_wallets()
    active  = [uid for uid, w in wallets.items() if int(w.get("check_until", 0)) > now]
    return active[:BATCH_LIMIT]


# ── Settings ───────────────────────────────────────────────────────────────────

def get_settings() -> dict:
    return get_data("server/crypto_settings") or {}


def save_settings(s: dict) -> None:
    replace_data("server/crypto_settings", s)


# ── Balance fetching ───────────────────────────────────────────────────────────

def sol_balance(address: str) -> int:
    """SOL balance in lamports. Returns -1 on error."""
    try:
        r = requests.post(
            SOL_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]},
            timeout=10,
        )
        data = r.json()
        if "error" in data or r.status_code != 200:
            print(f"[sol_balance] API error for {address}: {data}")
            return -1
        result = data.get("result")
        if result is None:
            return -1
        return int(result.get("value", 0))
    except Exception as e:
        print(f"[sol_balance] Exception for {address}: {e}")
        return -1


def _blockcypher_ltc_balance(address: str) -> int:
    """LTC confirmed balance in satoshis via BlockCypher (primary)."""
    try:
        params = {"token": BLOCKCYPHER_TOKEN} if BLOCKCYPHER_TOKEN else {}
        r = requests.get(f"{LTC_API}/{address}/balance", params=params, timeout=10)
        data = r.json()
        if r.status_code == 429 or r.status_code != 200 or "error" in data:
            return -1
        return int(data.get("final_balance", 0))
    except Exception:
        return -1


def _esplora_ltc_balance(address: str) -> int:
    """LTC confirmed balance in satoshis via litecoinspace Esplora (fallback)."""
    try:
        r = requests.get(f"{LTC_ESPLORA}/address/{address}", timeout=10)
        if r.status_code != 200:
            return -1
        d = r.json()
        funded = d.get("chain_stats", {}).get("funded_txo_sum", 0)
        spent  = d.get("chain_stats", {}).get("spent_txo_sum",  0)
        return int(funded - spent)
    except Exception:
        return -1


def ltc_balance(address: str) -> int:
    """LTC confirmed balance in satoshis. BlockCypher primary, Esplora fallback."""
    now = time.time()
    cached = _ltc_cache.get(address)
    if cached and now - cached[1] < _LTC_CACHE_TTL:
        return cached[0]
    bal = _blockcypher_ltc_balance(address)
    if bal < 0:
        bal = _esplora_ltc_balance(address)
    if bal >= 0:
        _ltc_cache[address] = (bal, now)
    return bal


def ltc_prefetch_balances(addresses: list[str]) -> None:
    """
    Fetch balances for all LTC addresses and populate the cache.
    Primary: BlockCypher batch endpoint (up to 100 per call).
    Fallback: litecoinspace Esplora (individual calls) for addresses that failed.
    """
    if not addresses:
        return
    now = time.time()
    failed: list[str] = []
    CHUNK = 100
    for i in range(0, len(addresses), CHUNK):
        chunk = addresses[i:i + CHUNK]
        joined = ";".join(chunk)
        try:
            params = {"token": BLOCKCYPHER_TOKEN} if BLOCKCYPHER_TOKEN else {}
            r = requests.get(f"{LTC_API}/{joined}/balance", params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    data = [data]
                done = set()
                for entry in data:
                    addr = entry.get("address")
                    if addr and "final_balance" in entry:
                        _ltc_cache[addr] = (int(entry["final_balance"]), now)
                        done.add(addr)
                failed.extend(a for a in chunk if a not in done)
            else:
                failed.extend(chunk)
        except Exception as e:
            print(f"[ltc_prefetch] BlockCypher error: {e}")
            failed.extend(chunk)
    # Fallback: Esplora for anything BlockCypher missed
    for address in failed:
        bal = _esplora_ltc_balance(address)
        if bal >= 0:
            _ltc_cache[address] = (bal, now)


# ── Exchange rates ─────────────────────────────────────────────────────────────

def get_rates() -> dict:
    """{'sol_usd': float, 'ltc_usd': float} with 60-second cache."""
    global _rate_cache
    now = time.time()
    if _rate_cache.get("_ts", 0) + _RATE_TTL > now and _rate_cache:
        return _rate_cache
    try:
        r = requests.get(
            COINGECKO,
            params={"ids": "solana,litecoin", "vs_currencies": "usd"},
            timeout=10,
        )
        d = r.json()
        _rate_cache = {
            "sol_usd": float(d.get("solana",   {}).get("usd", 0)),
            "ltc_usd": float(d.get("litecoin", {}).get("usd", 0)),
            "_ts":     now,
        }
    except Exception:
        pass
    return _rate_cache


def _usd_to_coins(usd: float) -> int:
    rates    = get_data("server/exchange_rates") or {}
    coin_usd = float(rates.get("coin_usd_rate", 0))
    if coin_usd <= 0:
        return 0
    return int(usd / coin_usd)


# ── Sweep (auto-transfer to main wallet) ──────────────────────────────────────

def sweep_sol(index: int, amount_lamports: int, to_address: str) -> str | None:
    """Transfer SOL from HD wallet[index] to to_address. Returns tx signature or None."""
    if amount_lamports <= SOL_FEE_LAMPORTS:
        return None
    try:
        import base64
        from bip_utils import Bip44, Bip44Coins, Bip44Changes
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.hash import Hash
        from solders.system_program import transfer, TransferParams
        from solders.message import Message
        from solders.transaction import Transaction

        ctx = (
            Bip44.FromSeed(_seed(), Bip44Coins.SOLANA)
            .Purpose().Coin().Account(index)
            .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        )
        kp = Keypair.from_seed(bytes(ctx.PrivateKey().Raw().ToBytes()))
        to_pk = Pubkey.from_string(to_address)
        send_lamports = amount_lamports - SOL_FEE_LAMPORTS

        r = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash", "params": [],
        }, timeout=10)
        bh = Hash.from_string(r.json()["result"]["value"]["blockhash"])

        ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pk, lamports=send_lamports))
        msg = Message.new_with_blockhash([ix], kp.pubkey(), bh)
        tx = Transaction([kp], msg, bh)

        encoded = base64.b64encode(bytes(tx)).decode()
        r2 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [encoded, {"encoding": "base64"}],
        }, timeout=15)
        result = r2.json().get("result")
        print(f"[Sweep SOL] idx={index} → {to_address}  sig={result}")
        return result
    except Exception as e:
        print(f"[Sweep SOL] Error idx={index}: {e}")
        return None


def _raw_p2pkh_ltc(priv_bytes: bytes, pub_bytes: bytes, src_addr: str,
                   utxos: list, to_address: str, fee_sat: int) -> str:
    """Build, sign and broadcast a raw P2PKH LTC transaction. Returns txid."""
    import struct, hashlib, ecdsa as _ecdsa

    def _sha256(x):     return hashlib.sha256(x).digest()
    def sha256d(x):     return _sha256(_sha256(x))
    def hash160(x):     return hashlib.new('ripemd160', _sha256(x)).digest()

    def varint(n):
        if n < 0xfd:        return bytes([n])
        if n < 0x10000:     return b'\xfd' + struct.pack('<H', n)
        if n < 0x100000000: return b'\xfe' + struct.pack('<I', n)
        return                      b'\xff' + struct.pack('<Q', n)

    def push(d):   return bytes([len(d)]) + d

    def der_encode(r, s, order):
        if s > order // 2:
            s = order - s
        def enc(n):
            b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
            if b[0] & 0x80:
                b = b'\x00' + b
            return b'\x02' + bytes([len(b)]) + b
        body = enc(r) + enc(s)
        return b'\x30' + bytes([len(body)]) + body

    def addr_to_script(addr):
        if addr.lower().startswith('ltc1'):
            CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
            pos = addr.lower().rfind('1')
            d5  = [CHARSET.find(c) for c in addr.lower()[pos+1:]]
            acc, bits, prog = 0, 0, []
            for v in d5[1:-6]:
                acc = (acc << 5) | v; bits += 5
                while bits >= 8:
                    bits -= 8; prog.append((acc >> bits) & 0xff)
            return bytes([0x00, 0x14]) + bytes(prog)
        ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        n = sum(58**i * ALPHA.index(c) for i, c in enumerate(reversed(addr)))
        return bytes([0x76, 0xa9, 0x14]) + n.to_bytes(25, 'big')[1:21] + bytes([0x88, 0xac])

    script_pk = bytes([0x76, 0xa9, 0x14]) + hash160(pub_bytes) + bytes([0x88, 0xac])
    to_script  = addr_to_script(to_address)
    total      = sum(u['value'] for u in utxos)
    net_sat    = total - fee_sat
    ins  = [{'txid': bytes.fromhex(u['txid'])[::-1], 'vout': u['vout'], 'value': u['value']}
            for u in utxos]
    outs = [{'value': net_sat, 'script': to_script}]

    def sighash(ins, outs, idx, sc):
        t  = struct.pack('<I', 1) + varint(len(ins))
        for j, inp in enumerate(ins):
            t += inp['txid'] + struct.pack('<I', inp['vout'])
            t += (varint(len(sc)) + sc) if j == idx else b'\x00'
            t += b'\xff\xff\xff\xff'
        t += varint(len(outs))
        for o in outs:
            t += struct.pack('<Q', o['value']) + varint(len(o['script'])) + o['script']
        t += struct.pack('<I', 0) + struct.pack('<I', 1)
        return sha256d(t)

    sk    = _ecdsa.SigningKey.from_string(priv_bytes, curve=_ecdsa.SECP256k1)
    ORDER = _ecdsa.SECP256k1.order
    raw_tx = struct.pack('<I', 1) + varint(len(ins))
    for i, inp in enumerate(ins):
        h   = sighash(ins, outs, i, script_pk)
        raw = sk.sign_digest_deterministic(h, hashfunc=hashlib.sha256,
                                           sigencode=_ecdsa.util.sigencode_string)
        r_v = int.from_bytes(raw[:32], 'big')
        s_v = int.from_bytes(raw[32:], 'big')
        sig = der_encode(r_v, s_v, ORDER) + b'\x01'
        ss  = push(sig) + push(pub_bytes)
        raw_tx += inp['txid'] + struct.pack('<I', inp['vout'])
        raw_tx += varint(len(ss)) + ss + b'\xff\xff\xff\xff'
    raw_tx += varint(len(outs))
    for o in outs:
        raw_tx += struct.pack('<Q', o['value']) + varint(len(o['script'])) + o['script']
    raw_tx += struct.pack('<I', 0)

    hex_tx = raw_tx.hex()
    # Broadcast via litecoinspace Esplora
    rb = requests.post(f'{LTC_ESPLORA}/tx',
                       data=hex_tx, headers={'Content-Type': 'text/plain'}, timeout=20)
    if rb.status_code == 200:
        return rb.text.strip()
    # Fallback: BlockCypher pushtx
    try:
        rb2 = requests.post(
            f'{BLOCKCYPHER_BASE}/txs/push',
            json={'tx': hex_tx},
            timeout=20,
        )
        if rb2.status_code == 201:
            return rb2.json().get('tx', {}).get('hash', '')
    except Exception:
        pass
    raise RuntimeError(f"Broadcast failed: esplora={rb.text[:200]}")


def _fetch_utxos_esplora(address: str) -> list:
    """Fetch UTXOs from litecoinspace Esplora ({txid, vout, value})."""
    r = requests.get(f'{LTC_ESPLORA}/address/{address}/utxo', timeout=15)
    if r.status_code != 200:
        return []
    return r.json()   # [{txid, vout, value, status:{confirmed,...}}, ...]


def sweep_ltc(index: int, to_address: str) -> str | None:
    """Sweep all LTC from HD wallet[index] to to_address. Returns txid or None."""
    try:
        from bip_utils import Bip44, Bip44Coins, Bip44Changes
        ctx = (Bip44.FromSeed(_seed(), Bip44Coins.LITECOIN)
               .Purpose().Coin().Account(index)
               .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
        src_addr  = ctx.PublicKey().ToAddress()
        priv_bytes = bytes(ctx.PrivateKey().Raw().ToBytes())
        pub_bytes  = bytes(ctx.PublicKey().RawCompressed().ToBytes())

        utxos = _fetch_utxos_esplora(src_addr)
        if not utxos:
            return None
        total = sum(u['value'] for u in utxos)
        if total <= LTC_FEE_SATOSHIS:
            return None

        txid = _raw_p2pkh_ltc(priv_bytes, pub_bytes, src_addr, utxos, to_address, LTC_FEE_SATOSHIS)
        print(f"[Sweep LTC] idx={index} → {to_address}  txid={txid}")
        return txid
    except Exception as e:
        print(f"[Sweep LTC] Error idx={index}: {e}")
        return None


# ── Deposit detection ──────────────────────────────────────────────────────────

# ── Balance helpers: house + treasury (fast) vs all HD wallets (slow) ─────────

def get_house_and_treasury_balances() -> dict:
    """
    Fetch SOL & LTC balances for the configured house (sweep) wallet
    and the treasury wallet.  Only 4 API calls — runs in < 2 seconds.
    Returns:
        {
          "house_sol_lamports": int, "house_ltc_satoshis": int,
          "treasury_sol_lamports": int, "treasury_ltc_satoshis": int,
          "house_sol_address": str, "house_ltc_address": str,
          "treasury_sol_address": str, "treasury_ltc_address": str,
        }
    """
    s = get_settings()
    h_sol_addr = s.get("sol_sweep_address", "") or ""
    h_ltc_addr = s.get("ltc_sweep_address", "") or ""

    t_sol_addr = t_ltc_addr = ""
    if TREASURY_MNEMONIC:
        try:
            t_sol_addr = get_treasury_address("SOL")
            t_ltc_addr = get_treasury_address("LTC")
        except Exception:
            pass

    def _safe_sol(addr):
        if not addr:
            return 0
        v = sol_balance(addr)
        return max(0, v)

    def _safe_ltc(addr):
        if not addr:
            return 0
        v = ltc_balance(addr)
        return max(0, v)

    return {
        "house_sol_lamports":    _safe_sol(h_sol_addr),
        "house_ltc_satoshis":    _safe_ltc(h_ltc_addr),
        "treasury_sol_lamports": _safe_sol(t_sol_addr),
        "treasury_ltc_satoshis": _safe_ltc(t_ltc_addr),
        "house_sol_address":     h_sol_addr,
        "house_ltc_address":     h_ltc_addr,
        "treasury_sol_address":  t_sol_addr,
        "treasury_ltc_address":  t_ltc_addr,
    }


# ── Total balance across ALL HD wallets (slow — hits every address) ────────────

def get_total_wallet_balances() -> dict:
    """
    Fetch current SOL & LTC balances for every HD wallet in the store.
    Uses a single batch BlockCypher call for all LTC addresses.
    Returns:
        {
          "sol_lamports": int,   # total across all user wallets
          "ltc_satoshis": int,
          "wallet_count": int,
          "sol_per_wallet": {address: lamports},
          "ltc_per_wallet": {address: satoshis},
        }
    """
    wallets = _all_wallets()
    sol_total = 0
    ltc_total = 0
    sol_per_wallet: dict[str, int] = {}
    ltc_per_wallet: dict[str, int] = {}

    # Prefetch all LTC addresses in one batch request
    ltc_addrs = [
        w["ltc"]["address"]
        for w in wallets.values()
        if "ltc" in w and w["ltc"].get("address")
    ]
    if ltc_addrs:
        ltc_prefetch_balances(ltc_addrs)

    for uid, w in wallets.items():
        sol_info = w.get("sol")
        if sol_info and sol_info.get("address"):
            bal = sol_balance(sol_info["address"])
            if bal >= 0:
                sol_total += bal
                sol_per_wallet[sol_info["address"]] = bal

        ltc_info = w.get("ltc")
        if ltc_info and ltc_info.get("address"):
            bal = ltc_balance(ltc_info["address"])
            if bal >= 0:
                ltc_total += bal
                ltc_per_wallet[ltc_info["address"]] = bal

    return {
        "sol_lamports":   sol_total,
        "ltc_satoshis":   ltc_total,
        "wallet_count":   len(wallets),
        "sol_per_wallet": sol_per_wallet,
        "ltc_per_wallet": ltc_per_wallet,
    }


def get_sweep_address_balances() -> dict:
    """
    Fetch current balances of the configured sweep (main) addresses.
    Returns {"sol_lamports": int, "ltc_satoshis": int}.
    """
    s = get_settings()
    sol_addr = s.get("sol_sweep_address", "")
    ltc_addr = s.get("ltc_sweep_address", "")
    result = {"sol_lamports": 0, "ltc_satoshis": 0}
    if sol_addr:
        bal = sol_balance(sol_addr)
        result["sol_lamports"] = max(0, bal)
    if ltc_addr:
        bal = ltc_balance(ltc_addr)
        result["ltc_satoshis"] = max(0, bal)
    return result


# ── Sweep log dispatcher ──────────────────────────────────────────────────────

_sweep_log_queue: list[dict] = []   # consumed by the cog's background task


def _dispatch_sweep_log(chain: str, amount: float, to_address: str, tx_id: str | None) -> None:
    """Queue a sweep log entry so the cog can post it to Discord."""
    _sweep_log_queue.append({
        "chain":      chain,
        "amount":     amount,
        "to_address": to_address,
        "tx_id":      tx_id or "unknown",
        "ts":         int(time.time()),
    })


def pop_sweep_logs() -> list[dict]:
    """Drain and return all pending sweep log entries."""
    logs = list(_sweep_log_queue)
    _sweep_log_queue.clear()
    return logs


def sweep_all_positive_wallets() -> list[dict]:
    """
    Sweep every HD wallet that has a balance above the network fee to the configured
    sweep addresses.  Runs independently of deposit detection so funds that arrived
    before auto_sweep was enabled (or after a failed sweep) are not left stranded.

    Returns list of sweep result dicts:
      {"chain", "amount_crypto", "to_address", "tx_id", "user_id"}
    """
    settings = get_settings()
    if not settings.get("auto_sweep", False):
        return []
    if not MNEMONIC:
        return []

    sol_sweep_addr = settings.get("sol_sweep_address", "")
    ltc_sweep_addr = settings.get("ltc_sweep_address", "")
    if not sol_sweep_addr and not ltc_sweep_addr:
        return []

    wallets = _all_wallets()
    results: list[dict] = []

    # Prefetch ALL LTC balances in one pass so individual ltc_balance() calls hit cache
    if ltc_sweep_addr and settings.get("ltc_enabled", True):
        all_ltc_addrs = [
            w["ltc"]["address"]
            for w in wallets.values()
            if "ltc" in w and w["ltc"].get("address")
        ]
        if all_ltc_addrs:
            ltc_prefetch_balances(all_ltc_addrs)

    for uid, w in wallets.items():
        idx = w.get("index", 0)

        # ── SOL ───────────────────────────────────────────────────────────────
        if sol_sweep_addr and settings.get("sol_enabled", True) and "sol" in w:
            try:
                bal = sol_balance(w["sol"]["address"])
                if bal > SOL_FEE_LAMPORTS:
                    sig = sweep_sol(idx, bal, sol_sweep_addr)
                    swept_sol = (bal - SOL_FEE_LAMPORTS) / 1e9
                    _dispatch_sweep_log("SOL", round(swept_sol, 6), sol_sweep_addr, sig)
                    results.append({
                        "chain":         "SOL",
                        "amount_crypto": round(swept_sol, 6),
                        "to_address":    sol_sweep_addr,
                        "tx_id":         sig or "unknown",
                        "user_id":       uid,
                    })
                    # Set last_balance to swept amount so deposit checker
                    # doesn't re-credit if TX is still pending next cycle
                    w["sol"]["last_balance"] = bal
                    wallets[uid] = w
            except Exception as e:
                print(f"[SweepAll SOL] uid={uid}: {e}")

        # ── LTC ───────────────────────────────────────────────────────────────
        if ltc_sweep_addr and settings.get("ltc_enabled", True) and "ltc" in w:
            try:
                bal = ltc_balance(w["ltc"]["address"])
                if bal > LTC_FEE_SATOSHIS:
                    txid = sweep_ltc(idx, ltc_sweep_addr)
                    swept_ltc = (bal - LTC_FEE_SATOSHIS) / 1e8
                    _dispatch_sweep_log("LTC", round(swept_ltc, 8), ltc_sweep_addr, txid)
                    results.append({
                        "chain":         "LTC",
                        "amount_crypto": round(swept_ltc, 8),
                        "to_address":    ltc_sweep_addr,
                        "tx_id":         txid or "unknown",
                        "user_id":       uid,
                    })
                    # Set last_balance to swept amount so deposit checker
                    # doesn't re-credit if TX is still pending next cycle
                    w["ltc"]["last_balance"] = bal
                    wallets[uid] = w
            except Exception as e:
                print(f"[SweepAll LTC] uid={uid}: {e}")

    if results:
        _save_wallets(wallets)

    return results


# ── Treasury wallet (hot wallet for withdrawals) ──────────────────────────────

def get_treasury_address(chain: str) -> str:
    """Return the treasury wallet address derived from TREASURY_MNEMONIC (account index 0)."""
    from bip_utils import Bip44, Bip44Coins, Bip44Changes
    seed = _treasury_seed()
    if chain == "SOL":
        ctx = (Bip44.FromSeed(seed, Bip44Coins.SOLANA)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
        return ctx.PublicKey().ToAddress()
    elif chain == "LTC":
        ctx = (Bip44.FromSeed(seed, Bip44Coins.LITECOIN)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
        return ctx.PublicKey().ToAddress()
    return ""


def send_sol_from_treasury(to_address: str, amount_lamports: int) -> str | None:
    """Send SOL from the treasury wallet (TREASURY_MNEMONIC) to to_address."""
    if amount_lamports <= SOL_FEE_LAMPORTS:
        raise ValueError(f"Amount ({amount_lamports} lamports) is below the fee threshold.")
    try:
        import base64
        from bip_utils import Bip44, Bip44Coins, Bip44Changes
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solders.hash import Hash
        from solders.system_program import transfer, TransferParams
        from solders.message import Message
        from solders.transaction import Transaction

        ctx = (Bip44.FromSeed(_treasury_seed(), Bip44Coins.SOLANA)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
        kp    = Keypair.from_seed(bytes(ctx.PrivateKey().Raw().ToBytes()))
        to_pk = Pubkey.from_string(to_address)
        net_lamports = amount_lamports - SOL_FEE_LAMPORTS

        r = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash", "params": [],
        }, timeout=10)
        bh = Hash.from_string(r.json()["result"]["value"]["blockhash"])

        ix  = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pk, lamports=net_lamports))
        msg = Message.new_with_blockhash([ix], kp.pubkey(), bh)
        tx  = Transaction([kp], msg, bh)

        encoded = base64.b64encode(bytes(tx)).decode()
        r2 = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [encoded, {"encoding": "base64"}],
        }, timeout=15)
        sig = r2.json().get("result")
        print(f"[Treasury SendSOL] → {to_address}  sig={sig}")
        return sig
    except Exception as e:
        print(f"[Treasury SendSOL] Error: {e}")
        raise


def send_ltc_from_treasury(to_address: str, amount_satoshis: int) -> str | None:
    """Send LTC from the treasury wallet (TREASURY_MNEMONIC) to to_address."""
    if amount_satoshis <= LTC_FEE_SATOSHIS:
        raise ValueError(f"Amount ({amount_satoshis} sat) is below the fee threshold.")
    try:
        from bip_utils import Bip44, Bip44Coins, Bip44Changes
        ctx = (Bip44.FromSeed(_treasury_seed(), Bip44Coins.LITECOIN)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
        src_addr   = ctx.PublicKey().ToAddress()
        priv_bytes = bytes(ctx.PrivateKey().Raw().ToBytes())
        pub_bytes  = bytes(ctx.PublicKey().RawCompressed().ToBytes())

        all_utxos = _fetch_utxos_esplora(src_addr)
        total     = sum(u['value'] for u in all_utxos) if all_utxos else 0
        if total < amount_satoshis:
            raise ValueError(f"Insufficient treasury LTC: {total} sat, need {amount_satoshis} sat.")

        # Pick UTXOs until we have enough
        selected, accumulated = [], 0
        for u in all_utxos:
            selected.append(u)
            accumulated += u['value']
            if accumulated >= amount_satoshis + LTC_FEE_SATOSHIS:
                break

        net_sat = amount_satoshis - LTC_FEE_SATOSHIS
        change  = accumulated - amount_satoshis

        # Build outputs: payment + optional change back to treasury
        import struct, hashlib, ecdsa as _ecdsa

        def _sha256(x):     return hashlib.sha256(x).digest()
        def sha256d(x):     return _sha256(_sha256(x))
        def hash160(x):     return hashlib.new('ripemd160', _sha256(x)).digest()
        def varint(n):
            if n < 0xfd:        return bytes([n])
            if n < 0x10000:     return b'\xfd' + struct.pack('<H', n)
            if n < 0x100000000: return b'\xfe' + struct.pack('<I', n)
            return                      b'\xff' + struct.pack('<Q', n)
        def push(d):   return bytes([len(d)]) + d
        def der_encode(r, s, order):
            if s > order // 2: s = order - s
            def enc(n):
                b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
                if b[0] & 0x80: b = b'\x00' + b
                return b'\x02' + bytes([len(b)]) + b
            body = enc(r) + enc(s)
            return b'\x30' + bytes([len(body)]) + body
        def addr_to_script(addr):
            if addr.lower().startswith('ltc1'):
                CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
                pos = addr.lower().rfind('1')
                d5  = [CHARSET.find(c) for c in addr.lower()[pos+1:]]
                acc, bits, prog = 0, 0, []
                for v in d5[1:-6]:
                    acc = (acc << 5) | v; bits += 5
                    while bits >= 8:
                        bits -= 8; prog.append((acc >> bits) & 0xff)
                return bytes([0x00, 0x14]) + bytes(prog)
            ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
            n = sum(58**i * ALPHA.index(c) for i, c in enumerate(reversed(addr)))
            return bytes([0x76, 0xa9, 0x14]) + n.to_bytes(25, 'big')[1:21] + bytes([0x88, 0xac])

        script_pk  = bytes([0x76, 0xa9, 0x14]) + hash160(pub_bytes) + bytes([0x88, 0xac])
        ins = [{'txid': bytes.fromhex(u['txid'])[::-1], 'vout': u['vout'], 'value': u['value']}
               for u in selected]
        outs = [{'value': net_sat, 'script': addr_to_script(to_address)}]
        if change > LTC_FEE_SATOSHIS:
            outs.append({'value': change, 'script': script_pk})  # change → treasury

        def sighash(ins, outs, idx, sc):
            t  = struct.pack('<I', 1) + varint(len(ins))
            for j, inp in enumerate(ins):
                t += inp['txid'] + struct.pack('<I', inp['vout'])
                t += (varint(len(sc)) + sc) if j == idx else b'\x00'
                t += b'\xff\xff\xff\xff'
            t += varint(len(outs))
            for o in outs:
                t += struct.pack('<Q', o['value']) + varint(len(o['script'])) + o['script']
            t += struct.pack('<I', 0) + struct.pack('<I', 1)
            return sha256d(t)

        sk    = _ecdsa.SigningKey.from_string(priv_bytes, curve=_ecdsa.SECP256k1)
        ORDER = _ecdsa.SECP256k1.order
        raw_tx = struct.pack('<I', 1) + varint(len(ins))
        for i, inp in enumerate(ins):
            h   = sighash(ins, outs, i, script_pk)
            raw = sk.sign_digest_deterministic(h, hashfunc=hashlib.sha256,
                                               sigencode=_ecdsa.util.sigencode_string)
            r_v = int.from_bytes(raw[:32], 'big')
            s_v = int.from_bytes(raw[32:], 'big')
            sig = der_encode(r_v, s_v, ORDER) + b'\x01'
            ss  = push(sig) + push(pub_bytes)
            raw_tx += inp['txid'] + struct.pack('<I', inp['vout'])
            raw_tx += varint(len(ss)) + ss + b'\xff\xff\xff\xff'
        raw_tx += varint(len(outs))
        for o in outs:
            raw_tx += struct.pack('<Q', o['value']) + varint(len(o['script'])) + o['script']
        raw_tx += struct.pack('<I', 0)

        hex_tx = raw_tx.hex()
        rb = requests.post(f'{LTC_ESPLORA}/tx',
                           data=hex_tx, headers={'Content-Type': 'text/plain'}, timeout=20)
        if rb.status_code == 200:
            txid = rb.text.strip()
        else:
            try:
                rb2 = requests.post(
                    f'{BLOCKCYPHER_BASE}/txs/push',
                    json={'tx': hex_tx},
                    timeout=20,
                )
                if rb2.status_code == 201:
                    txid = rb2.json().get('tx', {}).get('hash', '')
                else:
                    raise RuntimeError(f"Broadcast failed: esplora={rb.text[:150]}")
            except RuntimeError:
                raise
            except Exception as ex:
                raise RuntimeError(f"Broadcast failed: esplora={rb.text[:150]}  err={ex}")

        print(f"[Treasury SendLTC] → {to_address}  txid={txid}")
        return txid
    except Exception as e:
        print(f"[Treasury SendLTC] Error: {e}")
        raise


# ── Transaction confirmation checks ───────────────────────────────────────────

def check_sol_tx_finalized(signature: str) -> bool:
    """Return True if SOL transaction is finalized on-chain."""
    try:
        r = requests.post(SOL_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}],
        }, timeout=10)
        result = r.json().get("result", {}).get("value", [None])[0]
        return bool(result and result.get("confirmationStatus") == "finalized")
    except Exception:
        return False


def check_ltc_tx_confirmed(txid: str, min_confirmations: int = 3) -> bool:
    """Return True if LTC tx has at least min_confirmations."""
    try:
        r = requests.get(
            f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}",
            timeout=10,
        )
        return int(r.json().get("confirmations", 0)) >= min_confirmations
    except Exception:
        return False


def _credit_deposit(user_id: int, coins: int) -> None:
    """Credit coins, record deposit stats, race entry, and apply pending bonus."""
    from modules.player import Player
    from modules.deposit_credit import apply_pending_deposit_bonus

    p = Player(user_id)
    p.add_balance("real", coins)
    p.record_deposit(coins)
    try:
        import modules.race as race_engine
        race_engine.add_entry(str(user_id), coins, "deposit")
    except Exception:
        pass
    ok, bonus_amt = apply_pending_deposit_bonus(user_id, coins, consume=True)
    if ok and bonus_amt > 0:
        p.add_balance("real", bonus_amt)


def check_user_deposits(user_id: int) -> list[dict]:
    """
    Detect new deposits for user_id and credit coins.
    Returns list of credited deposit dicts.
    """
    settings = get_settings()
    if not settings.get("enabled", False):
        return []
    # NOTE: MNEMONIC is only required for auto-sweep, NOT for balance detection.

    uid     = str(user_id)
    wallets = _all_wallets()
    wallet  = wallets.get(uid)
    if not wallet:
        return []

    uid     = str(user_id)
    wallets = _all_wallets()
    wallet  = wallets.get(uid)
    if not wallet:
        return []

    # Refresh monitoring window so monitor task keeps checking this user
    wallets[uid]["check_until"] = int(time.time()) + MONITOR_TTL
    changed = True

    rates     = get_rates()
    min_usd   = float(settings.get("min_deposit_usd", 1.0))
    credited  = []

    # ── SOL ────────────────────────────────────────────────────────────────────
    sol_info = wallet.get("sol")
    if settings.get("sol_enabled", True) and sol_info:
        current  = sol_balance(sol_info["address"])
        last_bal = int(sol_info.get("last_balance", -1))

        if current >= 0:
            if last_bal < 0:
                # First ever check — treat as zero baseline so existing balance is credited
                last_bal = 0
                wallet["sol"]["last_balance"] = 0
            if current > last_bal:
                diff_sol   = (current - last_bal) / 1_000_000_000
                amount_usd = diff_sol * rates.get("sol_usd", 0)
                coins      = _usd_to_coins(amount_usd)

                if amount_usd >= min_usd and coins > 0:
                    _credit_deposit(user_id, coins)
                    credited.append({
                        "chain": "SOL", "symbol": "◎",
                        "amount_crypto": round(diff_sol, 6),
                        "amount_usd":    round(amount_usd, 2),
                        "coins":         coins,
                    })
                    # Auto-sweep (requires MNEMONIC)
                    if settings.get("auto_sweep", False) and MNEMONIC:
                        sol_sweep_addr = settings.get("sol_sweep_address", "")
                        if sol_sweep_addr:
                            try:
                                sig = sweep_sol(wallet["index"], current, sol_sweep_addr)
                                _dispatch_sweep_log("SOL", round(diff_sol, 6), sol_sweep_addr, sig)
                            except Exception as se:
                                print(f"[AutoSweep SOL] {se}")

                wallet["sol"]["last_balance"] = current
                changed = True

    # ── LTC ────────────────────────────────────────────────────────────────────
    ltc_info = wallet.get("ltc")
    if settings.get("ltc_enabled", True) and ltc_info:
        current  = ltc_balance(ltc_info["address"])
        last_bal = int(ltc_info.get("last_balance", -1))

        if current >= 0:
            if last_bal < 0:
                # First ever check — treat as zero baseline so existing balance is credited
                last_bal = 0
                wallet["ltc"]["last_balance"] = 0
            if current > last_bal:
                diff_ltc   = (current - last_bal) / 100_000_000
                amount_usd = diff_ltc * rates.get("ltc_usd", 0)
                coins      = _usd_to_coins(amount_usd)

                if amount_usd >= min_usd and coins > 0:
                    _credit_deposit(user_id, coins)
                    credited.append({
                        "chain": "LTC", "symbol": "Ł",
                        "amount_crypto": round(diff_ltc, 8),
                        "amount_usd":    round(amount_usd, 2),
                        "coins":         coins,
                    })
                    # Auto-sweep (requires MNEMONIC)
                    if settings.get("auto_sweep", False) and MNEMONIC:
                        ltc_sweep_addr = settings.get("ltc_sweep_address", "")
                        if ltc_sweep_addr:
                            try:
                                txid = sweep_ltc(wallet["index"], ltc_sweep_addr)
                                _dispatch_sweep_log("LTC", round(diff_ltc, 8), ltc_sweep_addr, txid)
                            except Exception as se:
                                print(f"[AutoSweep LTC] {se}")

                wallet["ltc"]["last_balance"] = current
                changed = True

    if changed:
        wallets[uid] = wallet
        _save_wallets(wallets)

    return credited
