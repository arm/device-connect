"""CLI for Device Connect device control.

This module provides the command-line interface for device operations:
- list: List registered devices
- register: Register a test device
- discover: Discover uncommissioned devices via mDNS
- commission: Commission a device with PIN
- interactive: Interactive REPL for device operations

Usage:
    python -m device_connect_server.devctl list [--compact]
    python -m device_connect_server.devctl register --id myDevice [--keepalive]
    python -m device_connect_server.devctl discover [--timeout 5]
    python -m device_connect_server.devctl commission <device_id> --pin 1234-5678
    python -m device_connect_server.devctl interactive
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional

from device_connect_edge.messaging import MessagingClient, create_client
from device_connect_edge.messaging.config import MessagingConfig

# Optional dependencies
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None

try:
    import zeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    zeroconf = None

# Configuration
MESSAGING_BACKEND = os.getenv("MESSAGING_BACKEND")
DEFAULT_BROKER_URL = os.getenv("NATS_URL", "nats://localhost:4222")
DEVICE_TTL = int(os.getenv("DEVICE_TTL", "15"))
TENANT = os.getenv("TENANT", "default")


def _create_messaging_client() -> tuple[MessagingClient, MessagingConfig]:
    """Create messaging client using configuration from environment."""
    config = MessagingConfig(backend=MESSAGING_BACKEND)

    if not config.servers:
        config.servers = [DEFAULT_BROKER_URL]

    messaging = create_client(config.backend)
    return messaging, config


def print_compact_devices(devices: List[Dict[str, Any]]) -> None:
    """Print devices in a compact format."""
    for dev in devices:
        print(dev.get("device_id", "<no-id>"))
        capabilities = dev.get("capabilities", {})

        for fn in capabilities.get("functions", []):
            name = fn.get("name", "<unknown>")
            desc = fn.get("description", "")
            print(f"  function {name} ({desc})")

        for ev in capabilities.get("events", []):
            name = ev.get("name", "<unknown>")
            desc = ev.get("description", "")
            print(f"  event {name} ({desc})")

        print()


async def list_devices(
    messaging_url: Optional[str] = None,
    compact: bool = False,
) -> List[Dict[str, Any]]:
    """List all registered devices."""
    messaging, config = _create_messaging_client()

    if messaging_url:
        config.servers = [messaging_url]

    try:
        await messaging.connect(
            servers=config.servers,
            credentials=config.credentials,
            tls_config=config.tls_config,
        )

        resp = await messaging.request(
            f"device-connect.{TENANT}.discovery",
            json.dumps({
                "jsonrpc": "2.0",
                "method": "discovery/listDevices",
                "id": "get-1",
            }).encode(),
            timeout=5,
        )

        devices = json.loads(resp.decode())["result"]["devices"]

        if compact:
            print_compact_devices(devices)
        else:
            print(json.dumps(devices, indent=2))

        return devices

    except Exception as e:
        print(f"Error listing devices: {e}", file=sys.stderr)
        raise
    finally:
        await messaging.close()


async def heartbeat_loop(
    messaging_url: Optional[str] = None,
    device_id: str = None,
) -> None:
    """Send periodic heartbeats for a device."""
    messaging, config = _create_messaging_client()

    if messaging_url:
        config.servers = [messaging_url]

    await messaging.connect(
        servers=config.servers,
        credentials=config.credentials,
        tls_config=config.tls_config,
    )

    subj = f"device-connect.{TENANT}.{device_id}.heartbeat"
    try:
        while True:
            msg = {"device_id": device_id, "ts": time.time()}
            await messaging.publish(subj, json.dumps(msg).encode())
            await asyncio.sleep(DEVICE_TTL / 3)
    except asyncio.CancelledError:
        pass
    finally:
        await messaging.close()


def start_heartbeat_thread(
    messaging_url: Optional[str] = None,
    device_id: str = None,
) -> tuple:
    """Start heartbeat loop in background thread."""
    loop = asyncio.new_event_loop()
    t = Thread(
        target=loop.run_until_complete,
        args=(heartbeat_loop(messaging_url, device_id),),
        daemon=True,
    )
    t.start()
    return loop, t


async def register_device(
    messaging_url: Optional[str] = None,
    device_id: str = None,
) -> None:
    """Register a test device."""
    messaging, config = _create_messaging_client()

    if messaging_url:
        config.servers = [messaging_url]

    await messaging.connect(
        servers=config.servers,
        credentials=config.credentials,
        tls_config=config.tls_config,
    )

    try:
        req_id = f"{device_id}-{int(time.time() * 1000)}"
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "registerDevice",
            "params": {
                "device_id": device_id,
                "device_ttl": DEVICE_TTL,
                "capabilities": {
                    "description": "Generic device stub for testing",
                    "functions": [],
                    "events": [],
                },
                "identity": {
                    "arch": "arm64",
                    "host_cpu": "sim.cpu",
                    "dram_mb": 2048,
                },
                "status": {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "location": "test-lab",
                },
            },
        }
        response = await messaging.request(
            f"device-connect.{TENANT}.registry",
            json.dumps(payload).encode(),
            timeout=2,
        )
        print(response.decode())
    finally:
        await messaging.close()


async def discover_devices(timeout: int = 5) -> List[Dict[str, Any]]:
    """Discover uncommissioned devices on local network using mDNS."""
    if not ZEROCONF_AVAILABLE:
        print("mDNS discovery requires zeroconf: pip install zeroconf", file=sys.stderr)
        return []

    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    class DeviceListener(ServiceListener):
        def __init__(self):
            self.devices = []

        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info:
                properties = {}
                for key, value in info.properties.items():
                    properties[key.decode("utf-8")] = value.decode("utf-8")

                device = {
                    "name": name,
                    "address": info.parsed_addresses()[0] if info.parsed_addresses() else None,
                    "port": info.port,
                    "properties": properties,
                }
                self.devices.append(device)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    print(f"Scanning for devices ({timeout}s)...")

    zc = Zeroconf()
    listener = DeviceListener()
    browser = ServiceBrowser(zc, "_device-connect._tcp.local.", listener)  # noqa: F841

    await asyncio.sleep(timeout)
    zc.close()

    if not listener.devices:
        print("  No devices found")
        return []

    print(f"\n  Found {len(listener.devices)} device(s):\n")
    for dev in listener.devices:
        props = dev["properties"]
        device_id = props.get("device_id", "unknown")
        state = props.get("state", "unknown")
        device_type = props.get("device_type", "unknown")

        print(f"  {device_id}")
        print(f"    Type:    {device_type}")
        print(f"    State:   {state}")
        print(f"    Address: {dev['address']}:{dev['port']}")
        print()

    return listener.devices


async def commission_device(
    device_id: str,
    pin: Optional[str] = None,
    qr_scan: bool = False,
    device_ip: Optional[str] = None,
    device_port: int = 5540,
    nats_urls: Optional[List[str]] = None,
    tenant: str = "default",
    output_dir: str = "security_infra/credentials",
) -> None:
    """Commission a device by providing PIN and generating JWT credentials."""
    if not AIOHTTP_AVAILABLE:
        print("Commissioning requires aiohttp: pip install aiohttp", file=sys.stderr)
        return

    from device_connect_server.security.commissioning import parse_pin

    if qr_scan:
        print("QR code scanning not yet implemented. Please use --pin instead.")
        return

    if not pin:
        print("PIN required. Use --pin <PIN> or --qr-scan", file=sys.stderr)
        return

    pin_clean = parse_pin(pin)

    if len(pin_clean) != 8 or not pin_clean.isdigit():
        print(f"Invalid PIN format. Expected 8 digits, got: {pin}", file=sys.stderr)
        return

    # Auto-discover device if IP not provided
    if not device_ip:
        print(f"Auto-discovering {device_id}...")
        devices = await discover_devices(timeout=3)

        matching = [d for d in devices if d["properties"].get("device_id") == device_id]
        if not matching:
            print(f"Device {device_id} not found. Please provide --device-ip", file=sys.stderr)
            return

        device_ip = matching[0]["address"]
        device_port = matching[0]["port"]
        print(f"   Found at {device_ip}:{device_port}")

    # Get device info
    print("\nRetrieving device information...")

    device_info_url = f"http://{device_ip}:{device_port}/info"
    device_nkey_public = None
    device_nkey_seed = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(device_info_url, timeout=5) as resp:
                device_info = await resp.json()
                device_nkey_public = device_info.get("nkey_public")
                device_nkey_seed = device_info.get("nkey_seed")
    except Exception as e:
        print(f"   Could not retrieve device info: {e}")
        return

    if not device_nkey_public:
        print("   Device did not provide NKey public key (required for JWT)")
        return

    # Generate operational credentials
    print(f"\nGenerating JWT credentials for {device_id}...")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    credentials = None

    print("   Using JWT authentication (NKey-based)")

    # Import JWT helper
    security_path = Path(__file__).parent.parent.parent / "security_infra"
    if str(security_path) not in sys.path:
        sys.path.insert(0, str(security_path))

    try:
        from jwt_helper import JWTHelper

        jwt_expiry = os.getenv("JWT_EXPIRY", "90d")

        helper = JWTHelper()
        jwt = helper.generate_user_jwt(
            device_id=device_id,
            nkey_public=device_nkey_public,
            expiry=jwt_expiry,
            tenant=tenant,
        )

        resolver_dir = str(security_path / "resolver")
        helper.export_jwt_to_resolver(device_id, jwt, resolver_dir)

        urls = nats_urls or [DEFAULT_BROKER_URL]
        uses_tls = any(url.startswith("tls://") for url in urls)

        credentials = {
            "device_id": device_id,
            "auth_type": "jwt",
            "tenant": tenant,
            "nats": {
                "urls": urls,
                "jwt": jwt,
                "nkey_seed": device_nkey_seed,
            },
        }

        if uses_tls:
            credentials["nats"]["tls"] = {
                "ca_file": os.getenv("NATS_TLS_CA_FILE", "security_infra/certs/ca-cert.pem")
            }

        print(f"   JWT generated (expiry: {jwt_expiry})")
        print("   JWT exported to resolver (NATS will auto-discover)")

    except Exception as e:
        print(f"   JWT generation failed: {e}")
        print("\n   Please ensure:")
        print("   1. NSC_HOME is set: export NSC_HOME=$PWD/security_infra/keys/nsc")
        print("   2. NKEYS_PATH is set: export NKEYS_PATH=$PWD/security_infra/keys/nkeys")
        print("   3. PATH includes nsc: export PATH=$PATH:$HOME/go/bin")
        return

    if credentials is None:
        print(f"\nFailed to generate credentials for {device_id}")
        return

    # Save credentials file
    creds_file = output_path / f"{device_id}.creds.json"
    with open(creds_file, "w") as f:
        json.dump(credentials, f, indent=2)
    creds_file.chmod(0o600)

    print(f"   Credentials saved: {creds_file}")

    # Commission device
    print(f"\nCommissioning device at {device_ip}:{device_port}...")

    try:
        async with aiohttp.ClientSession() as session:
            commission_url = f"http://{device_ip}:{device_port}/commission"
            payload = {"pin": pin_clean, "credentials": credentials}

            async with session.post(commission_url, json=payload, timeout=10) as resp:
                result = await resp.json()

                if result.get("success"):
                    print(f"\nDevice {device_id} commissioned successfully!")
                    print("\n   Authentication: JWT (dynamic)")
                    print(f"   JWT Expiry: {os.getenv('JWT_EXPIRY', '90d')}")
                    print("\n   Next steps:")
                    print("     1. Device will automatically connect to NATS")
                    print("     2. NATS will discover JWT from resolver")
                    print("     3. No manual configuration required!")
                    print(f"\n   Credentials file: {creds_file}")
                else:
                    error = result.get("error", "Unknown error")
                    print(f"\nCommissioning failed: {error}", file=sys.stderr)

    except Exception as e:
        print(f"\nCommissioning error: {e}", file=sys.stderr)


async def invoke_device_function(
    device_id: str,
    function_name: str,
    params: Optional[Dict[str, Any]] = None,
    messaging_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Invoke a function on a device."""
    messaging, config = _create_messaging_client()

    if messaging_url:
        config.servers = [messaging_url]

    await messaging.connect(
        servers=config.servers,
        credentials=config.credentials,
        tls_config=config.tls_config,
    )

    try:
        rpc = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": function_name,
            "params": params or {},
        }
        response = await messaging.request(
            f"device-connect.{TENANT}.{device_id}.cmd",
            json.dumps(rpc).encode(),
            timeout=30,
        )
        return json.loads(response.decode())
    finally:
        await messaging.close()


async def interactive_mode() -> None:
    """Run interactive REPL for device operations."""
    print("Device Connect - Interactive Mode")
    print("Commands: list, invoke <device> <function> [params], discover, help, quit")
    print("-" * 60)

    while True:
        try:
            line = input("dc> ").strip()
            if not line:
                continue

            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd in ("quit", "exit", "q"):
                print("Goodbye!")
                break

            elif cmd == "help":
                print("Available commands:")
                print("  list [--compact]           List registered devices")
                print("  invoke <device> <func>     Invoke a device function")
                print("  discover                   Discover devices via mDNS")
                print("  quit                       Exit interactive mode")

            elif cmd == "list":
                compact = "--compact" in line or "-c" in line
                await list_devices(compact=compact)

            elif cmd == "discover":
                await discover_devices()

            elif cmd == "invoke":
                if len(parts) < 3:
                    print("Usage: invoke <device_id> <function_name> [params_json]")
                    continue

                device_id = parts[1]
                rest = parts[2] if len(parts) > 2 else ""

                # Split function name from optional params
                func_parts = rest.split(maxsplit=1)
                function_name = func_parts[0]
                params = None

                if len(func_parts) > 1:
                    try:
                        params = json.loads(func_parts[1])
                    except json.JSONDecodeError:
                        print(f"Invalid JSON params: {func_parts[1]}")
                        continue

                result = await invoke_device_function(device_id, function_name, params)
                print(json.dumps(result, indent=2))

            else:
                print(f"Unknown command: {cmd}. Type 'help' for available commands.")

        except KeyboardInterrupt:
            print("\n")
            continue
        except EOFError:
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m device_connect_server.devctl",
        description="Device Connect Device Control CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list command
    p_list = sub.add_parser("list", help="List registered devices")
    p_list.add_argument("--broker", default=None, help="Broker URL")
    p_list.add_argument("--compact", "-c", action="store_true", help="Compact output")

    # register command
    p_reg = sub.add_parser("register", help="Register a device with registry")
    p_reg.add_argument("--id", required=True, help="Device ID")
    p_reg.add_argument("--broker", default=None, help="Broker URL")
    p_reg.add_argument("--keepalive", action="store_true", help="Start heartbeat loop")

    # discover command
    p_discover = sub.add_parser("discover", help="Discover uncommissioned devices")
    p_discover.add_argument("--timeout", type=int, default=5, help="Timeout in seconds")

    # commission command
    p_commission = sub.add_parser("commission", help="Commission a device with PIN")
    p_commission.add_argument("device_id", help="Device identifier")
    p_commission.add_argument("--pin", help="8-digit PIN (e.g., 1234-5678)")
    p_commission.add_argument("--qr-scan", action="store_true", help="Scan QR code")
    p_commission.add_argument("--device-ip", help="Device IP address")
    p_commission.add_argument("--device-port", type=int, default=5540, help="Device port")
    p_commission.add_argument("--broker-urls", nargs="+", help="Broker server URLs")
    p_commission.add_argument("--tenant", default=TENANT, help="Tenant name")
    p_commission.add_argument("--output-dir", default="security_infra/credentials", help="Credentials output")

    # interactive command
    sub.add_parser("interactive", help="Interactive mode for device operations")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """Main entry point for the CLI."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.cmd == "list":
        asyncio.run(list_devices(messaging_url=args.broker, compact=args.compact))

    elif args.cmd == "register":
        asyncio.run(register_device(messaging_url=args.broker, device_id=args.id))
        if args.keepalive:
            print("Starting heartbeat loop... Ctrl-C to stop.")
            loop, thread = start_heartbeat_thread(messaging_url=args.broker, device_id=args.id)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.stop()
                print("\nbye!")

    elif args.cmd == "discover":
        asyncio.run(discover_devices(timeout=args.timeout))

    elif args.cmd == "commission":
        asyncio.run(
            commission_device(
                device_id=args.device_id,
                pin=args.pin,
                qr_scan=args.qr_scan,
                device_ip=args.device_ip,
                device_port=args.device_port,
                nats_urls=args.broker_urls,
                tenant=args.tenant,
                output_dir=args.output_dir,
            )
        )

    elif args.cmd == "interactive":
        asyncio.run(interactive_mode())


if __name__ == "__main__":
    main()
