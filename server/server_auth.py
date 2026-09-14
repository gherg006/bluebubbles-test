# HTTP server handling authentication

import hashlib
import os
import subprocess

from flask import Flask, jsonify, request

from message_system import MessageSystem


class ServerAuth:
    # Checks and creates postgres table

    def __init__(self):
        self.database = os.getenv("BLUEBUBBLES_DB_NAME", "Bluebubbles_app")
        self.user = os.getenv("BLUEBUBBLES_DB_USER", "bluebubbles")
        self.password = os.getenv("BLUEBUBBLES_DB_PASSWORD", "12345")
        self.host = os.getenv("BLUEBUBBLES_DB_HOST", "127.0.0.1")

    @staticmethod
    def _hash_password(password):
        #hashes passwords
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _run_query(self, query, values):
        # Runs one query through the postgres client
        command = [
            "psql", "-X", "-q", "-t", "-A", "-h", self.host,
            "-U", self.user, "-d", self.database, "-v", "ON_ERROR_STOP=1",
        ]
        for name, value in values.items():
            command.extend(["-v", f"{name}={value}"])
        environment = os.environ.copy()
        environment["PGPASSWORD"] = self.password
        return subprocess.run(
            command,
            input=query,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def login(self, username, password):
        # Returns true when both creds match
        result = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' "
            "AND password_hash = :'password_hash' LIMIT 1;",
            {"username": username, "password_hash": self._hash_password(password)},
        )
        return result.returncode == 0 and result.stdout.strip() == "1"

    def register(self, username, password):
        # Saves new user unless that username is already in use
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

    def users(self):
        # Return every registered username for the client user list.
        result = self._run_query("SELECT username FROM users ORDER BY username;", {})
        if result.returncode != 0:
            return []
        return [name for name in result.stdout.splitlines() if name]


app = Flask(__name__)
auth = ServerAuth()
messages = MessageSystem(auth._run_query)


def _credentials():
    # Read the two fields in the json string
    data = request.get_json(silent=True) or {}
    return data.get("username", "").strip(), data.get("password", "")


@app.post("/login")
def login():
    # Handles login request from client
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    if auth.login(username, password):
        return jsonify(success=True, message="Logged in.")
    return jsonify(success=False, message="Incorrect username or password."), 401


@app.post("/register")
def register():
    # Handles registration request from client
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400

    success, message = auth.register(username, password)
    return jsonify(success=success, message=message), 201 if success else 409


@app.get("/users")
def users():
    # Return the registered usernames used by the client users list.
    return jsonify(users=auth.users())


@app.get("/messages")
def get_messages():
    # Return the selected conversation for the logged-in user.
    username = request.args.get("username", "").strip()
    other_user = request.args.get("with", "").strip()
    if not username or not other_user:
        return jsonify(messages=[]), 400
    return jsonify(messages=messages.conversation(username, other_user))


@app.post("/messages")
def send_message():
    # Save one plain-text message for another registered account.
    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "").strip()
    recipient = data.get("recipient", "").strip()
    content = data.get("content", "").strip()
    if not sender or not recipient or not content:
        return jsonify(success=False, message="Enter a recipient and message."), 400
    if messages.send(sender, recipient, content):
        return jsonify(success=True, message="Message sent."), 201
    return jsonify(success=False, message="The message could not be sent."), 400


if __name__ == "__main__":
    # Listen on the server's network address so the client machine can connect.
    app.run(host="0.0.0.0", port=5000, debug=False)
