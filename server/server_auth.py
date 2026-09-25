# HTTP server handling authentication

import hashlib
import json
import os
import subprocess

from flask import Flask, jsonify, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from message_system import MESSAGE_PAGE_SIZE, MessageSystem
from tls import tls_context


class ServerAuth:
    # Authentication and contact queries for the web API.

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

    def create_contacts_table(self):
        # Keep saved contacts and their read positions across client restarts.
        result = self._run_query(
            "BEGIN; "
            "CREATE TABLE IF NOT EXISTS chat_contacts ("
            "\"userID\" INTEGER NOT NULL REFERENCES users(\"userID\") ON DELETE CASCADE, "
            "contact_id INTEGER NOT NULL REFERENCES users(\"userID\") ON DELETE CASCADE, "
            "added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "last_read_message_id BIGINT NOT NULL DEFAULT 0, "
            "PRIMARY KEY (\"userID\", contact_id), "
            "CHECK (\"userID\" <> contact_id)"
            "); "
            "ALTER TABLE chat_contacts ADD COLUMN IF NOT EXISTS last_read_message_id BIGINT; "
            "UPDATE chat_contacts saved SET last_read_message_id = COALESCE(("
            "SELECT MAX(message_id) FROM messages "
            "WHERE sender_id = saved.contact_id AND recipient_id = saved.\"userID\""
            "), 0) WHERE saved.last_read_message_id IS NULL; "
            "ALTER TABLE chat_contacts ALTER COLUMN last_read_message_id SET DEFAULT 0; "
            "ALTER TABLE chat_contacts ALTER COLUMN last_read_message_id SET NOT NULL; "
            "COMMIT;",
            {},
        )
        return result.returncode == 0

    def contacts(self, username):
        # One indexed conversation scan per saved contact provides the sort metrics.
        result = self._run_query(
            "SELECT COALESCE(json_agg(json_build_object("
            "'name', contact.username, "
            "'added_at', (EXTRACT(EPOCH FROM saved.added_at) * 1000000)::bigint, "
            "'latest_message_id', COALESCE(activity.latest_message_id, 0), "
            "'message_count', COALESCE(activity.message_count, 0), "
            "'unread_count', COALESCE(activity.unread_count, 0)"
            ") ORDER BY saved.added_at, contact.username), '[]'::json) "
            "FROM chat_contacts saved "
            "JOIN users owner ON owner.\"userID\" = saved.\"userID\" "
            "JOIN users contact ON contact.\"userID\" = saved.contact_id "
            "LEFT JOIN LATERAL ("
            "SELECT MAX(message_id) AS latest_message_id, "
            "COUNT(*) AS message_count, "
            "COUNT(*) FILTER (WHERE sender_id = saved.contact_id "
            "AND recipient_id = saved.\"userID\" "
            "AND message_id > saved.last_read_message_id) AS unread_count "
            "FROM messages WHERE "
            "(sender_id = saved.\"userID\" AND recipient_id = saved.contact_id) "
            "OR (sender_id = saved.contact_id AND recipient_id = saved.\"userID\")"
            ") activity ON TRUE "
            "WHERE owner.username = :'username';",
            {"username": username},
        )
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return []

    def mark_contact_read(self, username, contact, through_id):
        # A read position only moves forward and never past this conversation.
        result = self._run_query(
            "UPDATE chat_contacts saved SET last_read_message_id = "
            "GREATEST(saved.last_read_message_id, LEAST(:'through_id'::bigint, "
            "COALESCE((SELECT MAX(message_id) FROM messages "
            "WHERE (sender_id = saved.\"userID\" AND recipient_id = saved.contact_id) "
            "OR (sender_id = saved.contact_id AND recipient_id = saved.\"userID\")), 0))) "
            "FROM users owner, users contact "
            "WHERE saved.\"userID\" = owner.\"userID\" "
            "AND saved.contact_id = contact.\"userID\" "
            "AND owner.username = :'username' AND contact.username = :'contact' "
            "RETURNING saved.last_read_message_id;",
            {"username": username, "contact": contact, "through_id": through_id},
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def add_contact(self, username, contact):
        # Save a sidebar contact once, provided both accounts exist.
        result = self._run_query(
            "WITH owner AS (SELECT \"userID\" FROM users WHERE username = :'username'), "
            "contact AS (SELECT \"userID\" FROM users WHERE username = :'contact'), "
            "saved AS ("
            "INSERT INTO chat_contacts (\"userID\", contact_id) "
            "SELECT owner.\"userID\", contact.\"userID\" FROM owner CROSS JOIN contact "
            "ON CONFLICT DO NOTHING"
            ") "
            "SELECT EXISTS (SELECT 1 FROM owner) AND EXISTS (SELECT 1 FROM contact);",
            {"username": username, "contact": contact},
        )
        return result.returncode == 0 and result.stdout.strip() == "t"

    def delete_contact(self, username, contact):
        # Delete only the selected contact that belongs to this account.
        result = self._run_query(
            "DELETE FROM chat_contacts "
            "WHERE \"userID\" = (SELECT \"userID\" FROM users WHERE username = :'username') "
            "AND contact_id = (SELECT \"userID\" FROM users WHERE username = :'contact') "
            "RETURNING contact_id;",
            {"username": username, "contact": contact},
        )
        return result.returncode == 0 and bool(result.stdout.strip())


app = Flask(__name__)
app.secret_key = os.getenv("BLUEBUBBLES_SESSION_SECRET")
if not app.secret_key:
    raise RuntimeError("BLUEBUBBLES_SESSION_SECRET must be configured.")
app.config.update(
    MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024 + 1024 * 1024,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)
auth = ServerAuth()
if not auth.create_contacts_table():
    raise RuntimeError("The contacts database migration could not be applied.")
messages = MessageSystem(auth._run_query)


def _credentials():
    # Read the two fields in the json string
    data = request.get_json(silent=True) or {}
    return data.get("username", "").strip(), data.get("password", "")


def _session_user():
    # Identity is established only by the signed, HTTPS-only session cookie.
    username = session.get("username")
    return username if isinstance(username, str) and username else None


def _require_session():
    username = _session_user()
    if username is None:
        return None, (jsonify(success=False, message="Log in to continue."), 401)
    return username, None


@app.errorhandler(RequestEntityTooLarge)
def file_too_large(_error):
    # Keep the client API JSON-only when Werkzeug rejects an oversized request.
    return jsonify(success=False, message="Files cannot be larger than 2 GB."), 413


@app.post("/login")
def login():
    # Handles login request from client
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    if auth.login(username, password):
        session.clear()
        session["username"] = username
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
    _, failure = _require_session()
    if failure:
        return failure
    return jsonify(users=auth.users())


@app.get("/contacts")
def get_contacts():
    # Return the contacts that should remain in this account's sidebar.
    username, failure = _require_session()
    if failure:
        return failure
    details = auth.contacts(username)
    return jsonify(contacts=[item["name"] for item in details], contact_details=details)


@app.post("/contacts/read")
def mark_contact_read():
    # Record the newest message this user has actually opened in a saved chat.
    username, failure = _require_session()
    if failure:
        return failure
    data = request.get_json(silent=True) or {}
    contact = data.get("contact", "")
    through_id = data.get("through_id")
    if (not isinstance(contact, str) or not contact.strip()
            or isinstance(through_id, bool) or not isinstance(through_id, int)
            or not 0 < through_id < 2**63):
        return jsonify(success=False, message="Invalid read position."), 400
    if auth.mark_contact_read(username, contact.strip(), through_id):
        return jsonify(success=True)
    return jsonify(success=False, message="The contact could not be marked read."), 404


@app.post("/contacts")
def add_contact():
    # Save an account to the caller's persistent sidebar.
    data = request.get_json(silent=True) or {}
    username, failure = _require_session()
    if failure:
        return failure
    contact = data.get("contact", "").strip()
    if not contact or username == contact:
        return jsonify(success=False, message="Choose another user to add."), 400
    if auth.add_contact(username, contact):
        return jsonify(success=True, message="Contact saved."), 201
    return jsonify(success=False, message="The contact could not be saved."), 400


@app.delete("/contacts")
def delete_contact():
    # Remove a saved sidebar contact only for the account that owns it.
    data = request.get_json(silent=True) or {}
    username, failure = _require_session()
    if failure:
        return failure
    contact = data.get("contact", "").strip()
    if not contact or username == contact:
        return jsonify(success=False, message="Choose a contact to delete."), 400
    if auth.delete_contact(username, contact):
        return jsonify(success=True, message="Contact deleted.")
    return jsonify(success=False, message="That contact could not be deleted."), 404


@app.get("/messages")
def get_messages():
    # Return the selected conversation for the logged-in user.
    username, failure = _require_session()
    if failure:
        return failure
    other_user = request.args.get("with", "").strip()
    if not other_user:
        return jsonify(messages=[]), 400
    before_value = request.args.get("before_id")
    after_value = request.args.get("after_id")
    if before_value is not None and after_value is not None:
        return jsonify(success=False, message="Choose one message cursor."), 400
    try:
        before_id = int(before_value) if before_value is not None else None
        after_id = int(after_value) if after_value is not None else None
    except ValueError:
        return jsonify(success=False, message="Invalid message cursor."), 400
    if any(value is not None and not 0 < value < 2**63 for value in (before_id, after_id)):
        return jsonify(success=False, message="Invalid message cursor."), 400
    page = messages.conversation(username, other_user, before_id=before_id, after_id=after_id)
    has_more = len(page) > MESSAGE_PAGE_SIZE
    page = page[:MESSAGE_PAGE_SIZE] if after_id is not None else page[-MESSAGE_PAGE_SIZE:]
    return jsonify(messages=page, has_more=has_more)


@app.post("/messages")
def send_message():
    # Save one encrypted message for another registered account.
    data = request.get_json(silent=True) or {}
    sender, failure = _require_session()
    if failure:
        return failure
    recipient = data.get("recipient", "").strip()
    content = data.get("content", "").strip()
    if not sender or not recipient or not content:
        return jsonify(success=False, message="Enter a recipient and message."), 400
    saved = messages.send(sender, recipient, content)
    if saved:
        return jsonify(success=True, message="Message sent.", saved_message=saved), 201
    return jsonify(success=False, message="The message could not be sent."), 400


@app.post("/files")
def upload_file():
    # Store a multipart file as an encrypted message and encrypted UUID-addressed blob.
    sender, failure = _require_session()
    if failure:
        return failure
    recipient = request.form.get("recipient", "").strip()
    checksum = request.form.get("checksum", "").strip().lower()
    uploaded_file = request.files.get("file")
    if not recipient or uploaded_file is None or not uploaded_file.filename:
        return jsonify(success=False, message="Choose a recipient and file."), 400
    saved = messages.send_file_stream(
        sender, recipient, uploaded_file.filename, uploaded_file.stream, checksum
    )
    if not saved:
        return jsonify(success=False, message="The file could not be uploaded or verified."), 400
    return jsonify(
        success=True, file_id=saved["file_id"], checksum=checksum,
        saved_message=saved, message="File uploaded.",
    ), 201


@app.get("/files/<file_id>")
def download_file(file_id):
    # The message participants alone can retrieve and decrypt the UUID-addressed file.
    username, failure = _require_session()
    if failure:
        return failure
    download = messages.retrieve_file_stream(username, file_id)
    if download is None:
        return jsonify(success=False, message="The file is unavailable."), 404
    contents, length, checksum = download
    response = app.response_class(contents, mimetype="application/octet-stream")
    response.content_length = length
    response.headers["X-Content-SHA256"] = checksum
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    # The API intentionally has no plaintext HTTP mode: every request uses TLS.
    port = int(os.getenv("BLUEBUBBLES_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=tls_context())
