import os
import json
import requests
from datetime import datetime, timezone
from flask import Flask, redirect, request, session, render_template, jsonify, send_from_directory
from pymongo import MongoClient
from google import genai
from google.genai import types

# Set absolute directory paths for Vercel Serverless runtime
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-key-fallback").strip()

# Session cookie configuration for modern HTTPS proxies (Vercel & Render)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Serverless environments (like Vercel) only allow writing to /tmp
BLUEPRINTS_DIR = "/tmp/blueprints" if os.getenv("VERCEL") else os.path.join(os.getcwd(), "blueprints")
os.makedirs(BLUEPRINTS_DIR, exist_ok=True)

CLIENT_ID = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()

SYSTEM_LOG_WEBHOOK_URL = os.getenv("SYSTEM_LOG_WEBHOOK_URL")
WEBSITE_WEBHOOK_URL = os.getenv("WEBSITE_WEBHOOK_URL",
