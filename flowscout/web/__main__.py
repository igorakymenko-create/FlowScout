"""python -m flowscout.web -- starts the local operator UI on :8787."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("flowscout.web.app:app", host="127.0.0.1", port=8787, reload=False)
