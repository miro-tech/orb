#!/usr/bin/env python3
"""
OrbVPN full pipeline (GitHub / oneshot):
  1) register / login
  2) farm ad credits (minutes)
  3) fetch servers → VLESS URIs (direct / ws / grpc / reality)

Creates a new output folder and saves:
  account.json
  balance.json
  servers.json
  uris_ws.txt
  uris_grpc.txt
  uris_direct.txt
  uris_reality.txt
  uris_all.txt

Reality SID is fixed to 0123456789abcdef.

Install:
  pip install requests

Run:
  python3 orb_oneshot.py
  python3 orb_oneshot.py -o orb_output
  python3 orb_oneshot.py -o orb_output --ads 40
  python3 orb_oneshot.py --email user@example.com --password 'YourPassword'
  python3 orb_oneshot.py -a ./orb_output/account.json --ads 30
  python3 orb_oneshot.py --limit 5 --ads 0
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://api.orbai.world/api/v1"
UA = "Dart/3.5 (dart:io)"
PASSWORD = "OrbTest123!Aa"
REALITY_SID = "0123456789abcdef"
TIMEOUT = 30
PROVIDERS = ("tapsell", "admob", "unity_ads", "yandex", "applovin")


def headers(token: str | None = None) -> dict:
    result = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: int = TIMEOUT,
    verify: bool = True,
) -> dict:
    response = requests.request(
        method,
        url,
        json=body,
        headers=headers(token),
        timeout=timeout,
        verify=verify,
    )
    if not response.ok:
        raise RuntimeError(
            f"{method} {url} → HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {response.text[:300]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return payload


def save_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def register(email: str, password: str) -> dict:
    payload = request_json(
        "POST",
        f"{API}/auth/register",
        body={"email": email, "password": password},
    )
    print(f"[*] register {email}")
    return payload


def login(email: str, password: str) -> dict:
    payload = request_json(
        "POST",
        f"{API}/auth/login",
        body={"email": email, "password": password},
    )
    print("[+] login successful")
    return payload


def create_account(email: str, password: str) -> dict:
    try:
        return register(email, password)
    except RuntimeError as exc:
        if "HTTP 409" in str(exc) or "already" in str(exc).lower():
            print("[*] account already exists; trying login")
            return login(email, password)
        raise


def token_of(payload: dict) -> str:
    data = payload.get("data") or payload
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected auth response: data is not an object")

    tokens = data.get("tokens") or data
    if not isinstance(tokens, dict):
        raise RuntimeError("Unexpected auth response: tokens is not an object")

    for key in ("access_token", "accessToken", "token"):
        value = tokens.get(key)
        if isinstance(value, str) and value.strip():
            return value

    raise RuntimeError("No access token in authentication response")


def ensure_token(account: dict, path: Path) -> str:
    token = account.get("access_token")
    if token:
        try:
            r = requests.get(
                f"{API}/tokens/balance-detailed",
                headers=headers(token),
                timeout=20,
            )
            if r.status_code == 200:
                return token
            print(f"[!] token check {r.status_code}, re-login…")
        except requests.RequestException:
            print("[!] token check failed, re-login…")
    payload = login(account["email"], account["password"])
    access = token_of(payload)
    account["access_token"] = access
    data = payload.get("data") or {}
    tokens = data.get("tokens") or data
    if isinstance(tokens, dict) and tokens.get("refresh_token"):
        account["refresh_token"] = tokens["refresh_token"]
    save_json(path, account)
    return access


def farm_ads(token: str, count: int) -> int:
    ok = 0
    for i in range(count):
        sess = None
        for provider in PROVIDERS:
            try:
                r = requests.post(
                    f"{API}/ad/session",
                    json={
                        "provider": provider,
                        "ad_type": "rewarded",
                        "platform": "android",
                    },
                    headers=headers(token),
                    timeout=20,
                )
                if r.status_code in (200, 201):
                    sess = r.json().get("data") or {}
                    if sess.get("id"):
                        break
            except requests.RequestException:
                continue
        if not sess or not sess.get("id"):
            print(f"  [{i + 1}/{count}] session fail")
            continue
        try:
            r2 = requests.post(
                f"{API}/ad/verify",
                json={"session_id": sess["id"], "token": sess.get("token")},
                headers=headers(token),
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"  [{i + 1}/{count}] verify err: {e}")
            continue
        if r2.status_code != 200:
            print(f"  [{i + 1}/{count}] verify {r2.status_code} {r2.text[:80]}")
            continue
        ok += 1
        reward = (r2.json().get("data") or {}).get("reward") or {}
        print(
            f"  [{ok}/{count}] balance={reward.get('new_credit_balance')} "
            f"+{reward.get('credits_earned', 1)}"
        )
        time.sleep(0.12)
    return ok


def get_balance(token: str) -> dict:
    payload = request_json("GET", f"{API}/tokens/balance-detailed", token=token)
    return payload.get("data") or {}


def vless_connect(host: str, token: str, body: dict | None = None) -> dict | None:
    try:
        return request_json(
            "POST",
            f"https://{host}:8443/vless/connect",
            token=token,
            body=body or {},
            timeout=25,
            verify=False,
        )
    except (requests.RequestException, RuntimeError) as exc:
        print(f"  connect {host}: {str(exc)[:180]}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OrbVPN: register → farm ads → VLESS URIs"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help="output directory (default: orb_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "-a",
        "--account",
        default="",
        help="reuse existing account.json (skip register)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="maximum number of servers to process; 0 = all",
    )
    parser.add_argument("--email", default="", help="existing or new account email")
    parser.add_argument(
        "--password",
        default=PASSWORD,
        help="account password (default: built-in test password)",
    )
    parser.add_argument(
        "--ads",
        type=int,
        default=25,
        help="number of ad verifies to farm (0 = skip farming)",
    )
    parser.add_argument(
        "--skip-uris",
        action="store_true",
        help="only farm ads, do not fetch URIs",
    )
    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit must be 0 or greater")
    if args.ads < 0:
        parser.error("--ads must be 0 or greater")

    if args.output:
        out = Path(args.output)
    elif args.account:
        out = Path(args.account).resolve().parent
    else:
        out = Path(time.strftime("orb_%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"[*] output directory: {out.resolve()}")

    account_path = Path(args.account) if args.account else (out / "account.json")

    # --- auth ---
    if args.account and account_path.is_file():
        account = json.loads(account_path.read_text(encoding="utf-8"))
        if not account.get("email") or not account.get("password"):
            raise SystemExit("account file must contain email + password")
        print(f"[*] reuse account {account['email']}")
        try:
            token = ensure_token(account, account_path)
        except Exception as exc:
            print(f"[ERROR] authentication failed: {exc}", file=sys.stderr)
            return 1
        email = account["email"]
        password = account["password"]
    else:
        email = args.email or f"orb.cli.{secrets.token_hex(4)}@gmail.com"
        password = args.password
        try:
            auth_payload = create_account(email, password)
            token = token_of(auth_payload)
        except Exception as exc:
            print(f"[ERROR] authentication failed: {exc}", file=sys.stderr)
            return 1
        device_id = str(uuid.uuid4())
        account = {
            "email": email,
            "password": password,
            "access_token": token,
            "device_id": device_id,
            "raw_auth": auth_payload,
        }
        save_json(account_path, account)
        print(f"[+] account saved: {account_path}")

    device_id = account.get("device_id") or str(uuid.uuid4())
    account["device_id"] = device_id

    # --- farm ads ---
    if args.ads > 0:
        print(f"[*] farming {args.ads} ads…")
        n = farm_ads(token, args.ads)
        print(f"[+] verified {n}/{args.ads}")
        try:
            balance = get_balance(token)
        except Exception:
            token = ensure_token(account, account_path)
            balance = get_balance(token)
        account["balance"] = balance
        save_json(account_path, account)
        save_json(out / "balance.json", balance)
        vpn_time = balance.get("vpn_time") or {}
        print(
            f"[+] vpn_time={vpn_time.get('formatted')} "
            f"({vpn_time.get('seconds')}s) credits={vpn_time.get('credits')}"
        )
    else:
        print("[*] skip ad farming (--ads 0)")
        try:
            balance = get_balance(token)
            save_json(out / "balance.json", balance)
            vpn_time = balance.get("vpn_time") or {}
            print(
                f"[+] vpn_time={vpn_time.get('formatted')} "
                f"credits={vpn_time.get('credits')}"
            )
        except Exception as exc:
            print(f"[!] balance: {exc}")

    if args.skip_uris:
        print("[*] skip URI fetch")
        print(f"[+] done → {out.resolve()}")
        return 0

    # --- servers + URIs ---
    try:
        servers_payload = request_json("GET", f"{API}/servers", token=token)
        server_data = servers_payload.get("data") or {}
        servers = server_data.get("servers") or []
        if not isinstance(servers, list):
            raise RuntimeError("The servers field is not a list")
        save_json(out / "servers.json", servers)
        print(f"[+] {len(servers)} servers")
    except Exception as exc:
        print(f"[ERROR] failed to retrieve servers: {exc}", file=sys.stderr)
        return 1

    selected = servers[: args.limit] if args.limit else servers
    ws_uris: list[str] = []
    grpc_uris: list[str] = []
    direct_uris: list[str] = []
    reality_uris: list[str] = []
    all_uris: list[str] = []

    for index, server in enumerate(selected, start=1):
        host = server.get("host_name") or server.get("public_ip")
        ip = server.get("public_ip")
        server_id = server.get("id")
        city = server.get("city") or "?"
        country = server.get("country") or "?"
        name = f"Orb-{city}-{country}-{server_id}".replace(" ", "")

        print(f"[{index}/{len(selected)}] {city}, {country}")

        if not host:
            print("  [!] server has no host_name/public_ip; skipped")
            continue

        if server_id is not None:
            try:
                request_json(
                    "POST",
                    f"{API}/connections/register",
                    token=token,
                    body={
                        "server_id": server_id,
                        "protocol": "vless",
                        "device_id": device_id,
                    },
                    timeout=20,
                )
            except Exception as exc:
                print(f"  [!] connection register: {str(exc)[:160]}")

        cfg_tcp = vless_connect(host, token, {"serverId": server_id, "transport": "tcp"})
        cfg_ws = vless_connect(host, token, {"transport": "ws"})
        cfg_grpc = vless_connect(host, token, {"transport": "grpc"})

        if ip and not cfg_ws:
            cfg_ws = vless_connect(ip, token, {"transport": "ws"})
        if ip and not cfg_grpc:
            cfg_grpc = vless_connect(ip, token, {"transport": "grpc"})

        configs = (cfg_tcp, cfg_ws, cfg_grpc)
        vless_uuid = next(
            (
                cfg.get("vlessUuid")
                for cfg in configs
                if isinstance(cfg, dict) and cfg.get("vlessUuid")
            ),
            None,
        )

        if not vless_uuid:
            print("  [!] no vlessUuid; skipped")
            continue

        # Direct TCP
        if ip:
            uri = (
                f"vless://{vless_uuid}@{ip}:10086"
                f"?encryption=none&security=none&type=tcp"
                f"#{quote(name + '-direct')}"
            )
            direct_uris.append(uri)
            all_uris.append(uri)

        # WebSocket over TLS
        ws_cfg = cfg_ws or {}
        ws_path = ws_cfg.get("wsPath") or "/api/v2/data"
        ws_port = ws_cfg.get("serverPort") or 443
        ws_host = ws_cfg.get("serverEndpoint") or host
        ws_dest = ip or ws_host

        uri = (
            f"vless://{vless_uuid}@{ws_dest}:{ws_port}"
            f"?encryption=none&security=tls&type=ws"
            f"&path={quote(ws_path, safe='')}"
            f"&host={quote(str(ws_host), safe='')}"
            f"&sni={quote(str(ws_host), safe='')}"
            f"&fp=chrome#{quote(name + '-ws')}"
        )
        ws_uris.append(uri)
        all_uris.append(uri)

        # gRPC over TLS
        grpc_cfg = cfg_grpc or {}
        grpc_service = grpc_cfg.get("grpcServiceName") or "api/v2/grpc"
        grpc_port = grpc_cfg.get("serverPort") or 443
        grpc_host = grpc_cfg.get("serverEndpoint") or host
        grpc_dest = ip or grpc_host

        uri = (
            f"vless://{vless_uuid}@{grpc_dest}:{grpc_port}"
            f"?encryption=none&security=tls&type=grpc"
            f"&serviceName={quote(str(grpc_service), safe='')}"
            f"&authority={quote(str(grpc_host), safe='')}"
            f"&sni={quote(str(grpc_host), safe='')}"
            f"&fp=chrome#{quote(name + '-grpc')}"
        )
        grpc_uris.append(uri)
        all_uris.append(uri)

        # Reality
        reality_cfg = cfg_tcp or {}
        public_key = (
            reality_cfg.get("realityPublicKey")
            or server.get("reality_public_key")
            or ""
        )
        profiles = reality_cfg.get("reality_profiles") or [
            {"name": "apple", "port": 2083, "sni": "www.apple.com"},
            {"name": "doh", "port": 2086, "sni": "cloudflare-dns.com"},
            {"name": "google", "port": 2087, "sni": "www.google.com"},
        ]
        flow = reality_cfg.get("flow") or "xtls-rprx-vision"
        reality_dest = ip or host

        if public_key:
            for profile in profiles:
                tag = profile.get("name") or "apple"
                port = profile.get("port") or 2083
                sni = profile.get("sni") or "www.apple.com"

                uri = (
                    f"vless://{vless_uuid}@{reality_dest}:{port}"
                    f"?encryption=none"
                    f"&flow={quote(str(flow), safe='-')}"
                    f"&security=reality"
                    f"&sni={quote(str(sni), safe='')}"
                    f"&fp=chrome"
                    f"&pbk={quote(str(public_key), safe='')}"
                    f"&sid={REALITY_SID}"
                    f"&type=tcp"
                    f"#{quote(name + '-reality-' + str(tag))}"
                )
                reality_uris.append(uri)
                all_uris.append(uri)
        else:
            print("  [!] Reality public key missing; Reality URI not generated")

        print(
            f"  uuid={vless_uuid[:8]}… "
            f"ws={ws_path} grpc={grpc_service}:{grpc_port} "
            f"reality={len(profiles) if public_key else 0}"
        )

    outputs = {
        "uris_ws.txt": ws_uris,
        "uris_grpc.txt": grpc_uris,
        "uris_direct.txt": direct_uris,
        "uris_reality.txt": reality_uris,
        "uris_all.txt": all_uris,
    }

    for filename, uris in outputs.items():
        content = "\n".join(uris)
        if content:
            content += "\n"
        (out / filename).write_text(content, encoding="utf-8")

    print()
    print("=" * 54)
    print(f"[+] OUTPUT: {out.resolve()}")
    print(f"[+] email: {email}")
    print(
        f"[+] ws={len(ws_uris)}  grpc={len(grpc_uris)}  "
        f"direct={len(direct_uris)}  reality={len(reality_uris)}"
    )
    print("[+] files: account.json balance.json servers.json " + " ".join(outputs.keys()))
    if grpc_uris:
        print(f"[+] grpc sample:\n    {grpc_uris[0][:180]}...")
    else:
        print("[!] grpc list EMPTY")
    print(f"[+] Reality SID: {REALITY_SID}")
    print("=" * 54)

    return 0 if all_uris else 1


if __name__ == "__main__":
    sys.exit(main())
