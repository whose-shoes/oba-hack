import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "oba_hack.app:app",
        host=os.environ.get("OBA_HOST", "0.0.0.0"),
        port=int(os.environ.get("OBA_PORT", "8000")),
    )
