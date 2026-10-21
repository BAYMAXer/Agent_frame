"""Start device B's browser executor without importing the planning framework."""
from launch import bootstrap


if __name__ == '__main__':
    bootstrap()
    from osworld_agent.remote_browser_server import main
    main()
