"""Launch the server-owned current production runtime."""

from watershed_memory.current.runtime_config import create_production_app

app = create_production_app()


if __name__ == "__main__":
    app.run()
