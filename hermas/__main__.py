import uvicorn

if __name__ == "__main__":
    uvicorn.run("hermas.web:create_app", factory=True, host="127.0.0.1", port=8765, reload=False, workers=1)
