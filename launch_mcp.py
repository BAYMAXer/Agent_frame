"""Absolute-path stdio/HTTP entry point for MCP hosts."""
from launch import bootstrap


if __name__ == "__main__":
    bootstrap()
    from osworld_agent.mcp_server import main

    main()
