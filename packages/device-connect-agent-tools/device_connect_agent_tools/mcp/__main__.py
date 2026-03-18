"""Entry point for MCP Bridge Server.

Usage:
    python -m device_connect_agent_tools.mcp

This starts the MCP Bridge Server which connects Claude Desktop to
Device Connect devices. Configuration is loaded from environment variables.

Environment Variables:
    ZENOH_CONNECT: Zenoh endpoint (default: tcp/localhost:7447)
    MESSAGING_URLS: Broker URLs, comma-separated (alternative to ZENOH_CONNECT)
    NATS_URL: NATS server URL (when using NATS backend)
    NATS_CREDENTIALS_FILE: Path to credentials file
    NATS_TLS_CA_FILE: Path to TLS CA certificate
    TENANT: Device Connect tenant (default: default)
    MCP_REFRESH_INTERVAL: Tool refresh interval in seconds (default: 30)
    MCP_REQUEST_TIMEOUT: Request timeout in seconds (default: 30)

Claude Desktop Configuration:
    Add to ~/Library/Application Support/Claude/claude_desktop_config.json:
    {
        "mcpServers": {
            "device-connect": {
                "command": "python",
                "args": ["-m", "device_connect_agent_tools.mcp"],
                "env": {
                    "ZENOH_CONNECT": "tcp/your-server:7447",
                    "DEVICE_CONNECT_ALLOW_INSECURE": "true"
                }
            }
        }
    }
"""

import asyncio
import logging
import sys


def main() -> None:
    """Run the MCP Bridge Server."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,  # MCP uses stdout for protocol, stderr for logs
    )

    # Reduce noise from libraries
    logging.getLogger("nats").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Starting Device Connect MCP Bridge")

    try:
        from device_connect_agent_tools.mcp.bridge import run_bridge
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        logger.info("Shutting down")
    except ImportError as e:
        logger.error(f"Import error: {e}")
        logger.error("Make sure fastmcp is installed: pip install 'device-connect-agent-tools[mcp]'")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
