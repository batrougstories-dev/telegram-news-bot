import os
import threading
import feedparser
import requests
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot v5 with feedparser"

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
