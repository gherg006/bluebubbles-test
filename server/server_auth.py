"""Small HTTP server that handles BlueBubbles login and registration."""

import hashlib
import os
import subprocess

from flask import Flask, jsonify, request


class ServerAuth:
    """Checks and creates users in the existing PostgreSQL users table."""

    def __init__(self):
        self.database = os.getenv("BLUEBUBBLES_DB_NAME", "Bluebubbles_app")
        self.user = os.getenv("BLUEBUBBLES_DB_USER", "bluebubbles")
        self.password = os.getenv("BLUEBUBBLES_DB_PASSWORD", "12345")
        self.host = os.getenv("BLUEBUBBLES_DB_HOST", "127.0.0.1")

    @staticmethod
    def _hash_password(password):
        """Hash passwords as requested, without adding a salt."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _run_query(self, query, values):
        """Run one parameterised query through the server's psql client."""
        command = [
            "psql", "-X", "-q", "-t", "-A", "-h", self.host,
            "-U", self.user, "-d", self.database, "-v", "ON_ERROR_STOP=1",
        ]
        for name, value in values.items():
            command.extend(["-v", f"{name}={value}"])
        command.extend(["-c", query])

        environment = os.environ.copy()
        environment["PGPASSWORD"] = self.password
        return subprocess.run(
            command, capture_output=True, text=True, env=environment, timeout=10
        )

    def login(self, username, password):
        """Return True only when both saved credentials match."""
        result = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' "
            "AND password_hash = :'password_hash' LIMIT 1;",
            {"username": username, "password_hash": self._hash_password(password)},
        )
        return result.returncode == 0 and result.stdout.strip() == "1"

    def register(self, username, password):
        """Save a new user unless that username already exists."""
        values = {"username": username, "password_hash": self._hash_password(password)}
        exists = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' LIMIT 1;", values
        )
        if exists.returncode != 0:
            return False, "The server could not save the account."
        if exists.stdout.strip() == "1":
            return False, "That username is already in use."

        saved = self._run_query(
            "INSERT INTO users (username, password_hash) "
            "VALUES (:'username', :'password_hash');", values
        )
        if saved.returncode != 0:
            return False, "The server could not save the account."
        return True, "Account created."


app = Flask(__name__)
auth = ServerAuth()


def _credentials():
    """Read the two required fields from an incoming JSON request."""
    data = request.get_json(silent=True) or {}
    return data.get("username", "").strip(), data.get("password", "")


@app.post("/login")
def login():
    """Handle a login request from the client."""
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    if auth.login(username, password):
        return jsonify(success=True, message="Logged in.")
    return jsonify(success=False, message="Incorrect username or password."), 401


@app.post("/register")
def register():
    """Handle a registration request from the client."""
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400

    success, message = auth.register(username, password)
    return jsonify(success=success, message=message), 201 if success else 409


if __name__ == "__main__":
    # Listen on the server's network address so the client machine can connect.
    app.run(host="0.0.0.0", port=5000, debug=False)
