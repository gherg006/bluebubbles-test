#HTTPS server handling authentication

import hashlib
import json
import os
import subprocess

#Imports standard libaries I was using before.
from flask import Flask, jsonify, request, session
#New library for utilities specifically exceptions.
from werkzeug.exceptions import RequestEntityTooLarge

#Imports page size that is set in another file.
from message_system import MESSAGE_PAGE_SIZE, MessageSystem
from tls import tls_context


class ServerAuth:
    #Checks and creates postgres table (if not already present).
    #Also handles other things like login and registration.

    def __init__(self):
        #Provides the database connection details from environment variables, otherwise defaults are used.
        self.database = os.getenv("BLUEBUBBLES_DB_NAME", "Bluebubbles_app")
        self.user = os.getenv("BLUEBUBBLES_DB_USER", "bluebubbles")
        self.password = os.getenv("BLUEBUBBLES_DB_PASSWORD", "12345")
        self.host = os.getenv("BLUEBUBBLES_DB_HOST", "127.0.0.1")
        #All of the above are used to connect to the DB.
        #Credentials are stored in a .env file, and use the defaultcredentials if the .env file is not found.

    @staticmethod           #Can be called without making the class an instance.
    def _hash_password(password):
        #Hashes passwords using sha256
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _run_query(self, query, values):
        #These are the flags that can be used that are within the list.
        #The other things are the host, user and database that are being used for the query.
        command = [
            "psql", "-X", "-q", "-t", "-A", "-h", self.host,
            "-U", self.user, "-d", self.database, "-v", "ON_ERROR_STOP=1",
        ]
        for name, value in values.items():
            #This adds values to the dictionary that can be used in the query.
            command.extend(["-v", f"{name}={value}"])
        #Copies in the environment variables and injects the password securely into PGPASSWORD.
        environment = os.environ.copy()
        environment["PGPASSWORD"] = self.password
        #This runs the actual query then returns the result, whether that is an output, or a boolean outcome.
        #It is passed in as a simple string, and has a 10 second timeout to avoid hanging if it takes too long.
        return subprocess.run(
            command,
            input=query,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def login(self, username, password):
        #Returns true when both creds match.
        #Takes the creds that are inputted and checks them against the database.
        result = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' "
            "AND password_hash = :'password_hash' LIMIT 1;",
            {"username": username, "password_hash": self._hash_password(password)},
        )
        #Checks what the database returned and returns true if the creds match, otherwise false.
        return result.returncode == 0 and result.stdout.strip() == "1"

    def register(self, username, password):
        #Saves new user unless that username is already in use.
        values = {"username": username, "password_hash": self._hash_password(password)}
        #Checks if the username already exists.
        exists = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' LIMIT 1;", values
        )
        #If there is an error with the DB, it will return false and a message on the login screen.
        if exists.returncode != 0:
            return False, "The server could not save the account."
        #If the username is in use, it will return false too, as well as a message on the login screen.
        if exists.stdout.strip() == "1":
            return False, "That username is already in use."
        #Takes the user's username and password, hashes it, then saves it to the DB.
        saved = self._run_query(
            "INSERT INTO users (username, password_hash) "
            "VALUES (:'username', :'password_hash');", values
        )
        #This is then a final check to ensure that the account was actually saved in the DB.
        #This ensures that accounts are actually created and not just assumed to be created.
        if saved.returncode != 0:
            return False, "The server could not save the account."
        return True, "Account created."

    def users(self):        #Return every registered username for the client user list.
        #SQL statement is run then written to result.
        result = self._run_query("SELECT username FROM users ORDER BY username;", {})
        #Validation added here to ensure something was actually returned.
        if result.returncode != 0:
            return []
        return [name for name in result.stdout.splitlines() if name]

    def create_contacts_table(self):        #Create the per-user contact list used to restore chat contacts.
        #Query is build to build the table.
        #It includes userid, contactid, timestamp, pkey, contacts (new), last_read_message (new). 
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
        #Then returns whether a contact was created or not.
        return result.returncode == 0

    def contacts(self, username):       #Gets all of a selected contacts.
        #The whole query here is new essentially as json is integrated.
        #It gets the json object for each contact, then aggregates them in a query.
        #It runs the query for the table to get all the contact relations.
        #Then it saves the result.
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
        #Checks whether the query was successful.
        if result.returncode != 0:
            return []
        
        try:
            #Returns the output line by line, filtering out any empty strings.
            #This ends up as a clean list of usernames as contacts for one user.
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return []

    def mark_contact_read(self, username, contact, through_id):
        #A read position only moves forward and never past this conversation.
        #The query is constructed to update the table to mark a message as read.
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
        #Returns if it was a success or not.
        return result.returncode == 0 and bool(result.stdout.strip())

    def add_contact(self, username, contact):
        #Save a sidebar contact, as long as both users exist.
        #An insert query is ran to insert the new user as a contact.
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
        #Delete only the selected contact that belongs to the active specific account.
        #A remove query is ran that removes that record from the table.
        result = self._run_query(
            "DELETE FROM chat_contacts "
            "WHERE \"userID\" = (SELECT \"userID\" FROM users WHERE username = :'username') "
            "AND contact_id = (SELECT \"userID\" FROM users WHERE username = :'contact') "
            "RETURNING contact_id;",
            {"username": username, "contact": contact},
        )
        return result.returncode == 0 and bool(result.stdout.strip())


app = Flask(__name__)            #Creates an instance of the flask web app.
app.secret_key = os.getenv("BLUEBUBBLES_SESSION_SECRET")
if not app.secret_key:
    #If there is no main secret key, the program won't start.
    raise RuntimeError("BLUEBUBBLES_SESSION_SECRET must be configured.")
#Updates the configuration to the correct parameters.
app.config.update(
    MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024 + 1024 * 1024,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)
auth = ServerAuth()         #Creates an instance of the server authentication class.
if not auth.create_contacts_table():
    #It does the contacts DB migration which is crucial for the contacts system.
    #If it fails, the program fails to run and an error is raised.
    raise RuntimeError("The contacts database migration could not be applied.")
messages = MessageSystem(auth._run_query)       #Initialises the message system.


def _credentials():     #Helper function to read the username and password from a request.
    #Read the two fields in the json string.
    data = request.get_json(silent=True) or {}
    #Returns the username and password as a tuple, as well as stripping any whitespace from the string.
    return data.get("username", "").strip(), data.get("password", "")

#Cookie logic to stop users faking session authentication.
def _session_user():
    #Identity is established only by the signed, HTTPS-only session cookie.
    username = session.get("username")
    #Returns the username to show that the session is valid.
    return username if isinstance(username, str) and username else None


def _require_session():
    #Gets the username from the authenticated session.
    username = _session_user()
    if username is None:
        #If there is no username, then show a screen to login.
        return None, (jsonify(success=False, message="Log in to continue."), 401)
    return username, None


@app.errorhandler(RequestEntityTooLarge)    #Catches flask exceptions.
def file_too_large(_error):
    #Keep the client API JSON-only when Werkzeug rejects an oversized request.
    #Standard 413 message being the payload is too large.
    return jsonify(success=False, message="Files cannot be larger than 2 GB."), 413


@app.post("/login")      #Login endpoint for the client to send a request to.
def login():        #Handles login request from client
    #Checks if the username and password are empty, and returns an error message if they are.
    username, password = _credentials()
    if not username or not password:
        #If nothing there then prompt user for username and/or password.
        return jsonify(success=False, message="Enter a username and password."), 400
    if auth.login(username, password):
        #Authenticates the session if username and password match.
        session.clear()
        session["username"] = username
        return jsonify(success=True, message="Logged in.")
    return jsonify(success=False, message="Incorrect username or password."), 401
    #It does all the server side checks that are possible, before sending the result back to the client.
    #Prevents errors showing to the client and malicious requests being processed.


@app.post("/register")      #Register endpoint for the client to send a request to.
def register():             #Handles registration request from client.
    username, password = _credentials()
    #This does the same empty checks as the login endpoint, returning a message if the fields are empty.
    #It parses it to json then checks it against the DB.
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    #Only when the creds are not empty, and the username is not in use, then it will return a success.
    #This then triggers a client message to say that the account has been created, and they can log in.

    success, message = auth.register(username, password)
    #This is the final return check to ensure that the account was created.
    return jsonify(success=success, message=message), 201 if success else 409


@app.get("/users")      #Users list endpoint for the client to send a request to.
def users():            
    #Does session authentication first.
    _, failure = _require_session()
    if failure:
        return failure
    #Return the registered usernames used by the client users list using a parsed json string.
    return jsonify(users=auth.users())


@app.get("/contacts")       #Contacts endpoint for a client to send a request to.
def get_contacts():
    #Return the contacts that should remain in this account's sidebar.
    #Checks session authentication first as well.
    username, failure = _require_session()
    if failure:
        return failure
    
    details = auth.contacts(username)
    #Returns json response of the names and and metadata for them.
    #Some of this is needed for the contact sorting methods.
    return jsonify(contacts=[item["name"] for item in details], contact_details=details)


@app.post("/contacts/read")         #For read messages there is a seperate endpoint.
def mark_contact_read():
    #Record the newest message this user has actually opened in a saved chat.
    username, failure = _require_session()
    if failure:
        return failure
    #Assigns all variables to be used by method.
    data = request.get_json(silent=True) or {}
    contact = data.get("contact", "")
    through_id = data.get("through_id")
    #Validate the contact is not an empty string.   
    if (not isinstance(contact, str) or not contact.strip()
            or isinstance(through_id, bool) or not isinstance(through_id, int)
            or not 0 < through_id < 2**63):
        return jsonify(success=False, message="Invalid read position."), 400
    #Attempt to mark the contact read in the authentication backend and return status.
    if auth.mark_contact_read(username, contact.strip(), through_id):
        return jsonify(success=True)
    return jsonify(success=False, message="The contact could not be marked read."), 404


@app.post("/contacts")
def add_contact():
    #Save an account to the caller's persistent sidebar in the DB.
    data = request.get_json(silent=True) or {}
    #Username and contact are passed in.
    username, failure = _require_session()
    #Ensures session is authenticated.
    if failure:
        return failure
    contact = data.get("contact", "").strip()
    #Does checks to ensure contact isn't repeated.
    #If everything suceeds, the contact is saved to the DB.
    #Otherwise an exception is thrown.
    if not contact or username == contact:
        return jsonify(success=False, message="Choose another user to add."), 400
    if auth.add_contact(username, contact):
        return jsonify(success=True, message="Contact saved."), 201
    #A catchall is below to get anything that is not a standard error.
    return jsonify(success=False, message="The contact could not be saved."), 400


@app.delete("/contacts")
def delete_contact():
    #Remove a saved sidebar contact only for the account that owns it.
    data = request.get_json(silent=True) or {}
    #Similar thing to adding contacts, it takes both the owner and other user.
    username, failure = _require_session()
    #Session authentication to ensure no unauthorised contact deletion.
    if failure:
        return failure
    contact = data.get("contact", "").strip()
    if not contact or username == contact:
        return jsonify(success=False, message="Choose a contact to delete."), 400
    if auth.delete_contact(username, contact):
        #Returns a deleted message if everything goes correctly.
        return jsonify(success=True, message="Contact deleted.")
    #Catches all exceptions not listed above.
    return jsonify(success=False, message="That contact could not be deleted."), 404


@app.get("/messages")       #The messages endpoint a client can send a request to.
def get_messages():
    #Return the selected conversation for the logged-in user.
    #First it ensures that the user has correct authentication though.
    username, failure = _require_session()
    if failure:
        return failure
    #Retrieve and sanitise target convo with target contact.
    other_user = request.args.get("with", "").strip()
    if not other_user:
        #Return a 400 if no contact is specified.
        return jsonify(messages=[]), 400 
    #Exact cursor values from client.
    before_value = request.args.get("before_id")
    after_value = request.args.get("after_id")
    #Ensure both cursors are not given at the same time.
    if before_value is not None and after_value is not None:
        return jsonify(success=False, message="Choose one message cursor."), 400
    try:
        #Convert cursor values to integers.
        before_id = int(before_value) if before_value is not None else None
        after_id = int(after_value) if after_value is not None else None
    #Validate message IDs fall within the expected range.
    except ValueError:
        return jsonify(success=False, message="Invalid message cursor."), 400
    if any(value is not None and not 0 < value < 2**63 for value in (before_id, after_id)):
        return jsonify(success=False, message="Invalid message cursor."), 400
    #Fetch the conversation page from backend using cursor.
    page = messages.conversation(username, other_user, before_id=before_id, after_id=after_id)
    #Determine if more messages exist past the page limit.
    has_more = len(page) > MESSAGE_PAGE_SIZE
    #Split the page depending if it is loading forward or backwards.
    page = page[:MESSAGE_PAGE_SIZE] if after_id is not None else page[-MESSAGE_PAGE_SIZE:]
    #Returns json formatted messages list.
    return jsonify(messages=page, has_more=has_more)


@app.post("/messages")
def send_message():
    #Save an encrypted message for another registered account.
    #The fields are saved to local variables.
    data = request.get_json(silent=True) or {}
    sender, failure = _require_session()
    #Auth checks done first.
    if failure:
        return failure
    #Sanitises inputs to get the content and recipient.
    recipient = data.get("recipient", "").strip()
    content = data.get("content", "").strip()
    if not sender or not recipient or not content:
        #Ensures it is not empty.
        return jsonify(success=False, message="Enter a recipient and message."), 400
    saved = messages.send(sender, recipient, content)
    if saved:
        #If it all works, then the message is sent.
        return jsonify(success=True, message="Message sent.", saved_message=saved), 201
    return jsonify(success=False, message="The message could not be sent."), 400


@app.post("/files")
def upload_file():
    #Store a multipart file as an encrypted message and encrypted UUID-addressed blob.
    sender, failure = _require_session()
    if failure:
        return failure
    #The form data is sanitised to get the recipient and the file.
    recipient = request.form.get("recipient", "").strip()
    checksum = request.form.get("checksum", "").strip().lower()
    uploaded_file = request.files.get("file")
    #Validates that both a recipient and valid file were provided.
    if not recipient or uploaded_file is None or not uploaded_file.filename:
        return jsonify(success=False, message="Choose a recipient and file."), 400
    #Stream the file upload through messaging backend.
    saved = messages.send_file_stream(
        sender, recipient, uploaded_file.filename, uploaded_file.stream, checksum
    )
    if not saved:
        return jsonify(success=False, message="The file could not be uploaded or verified."), 400
    #Return success response with fileID and checksum on successful upload.
    return jsonify(
        success=True, file_id=saved["file_id"], checksum=checksum,
        saved_message=saved, message="File uploaded.",
    ), 201


@app.get("/files/<file_id>")
def download_file(file_id):
    #The message participants alone can retrieve and decrypt the UUID-addressed file.
    username, failure = _require_session()
    #User authentication.
    if failure:
        return failure
    #Retrieve the file stream metadata.
    download = messages.retrieve_file_stream(username, file_id)
    if download is None:
        return jsonify(success=False, message="The file is unavailable."), 404
    contents, length, checksum = download
    #Builds a stream response with context length and security headers.
    #It is streamed in octets.
    response = app.response_class(contents, mimetype="application/octet-stream")
    response.content_length = length
    response.headers["X-Content-SHA256"] = checksum
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    #The API intentionally has no plaintext HTTP mode: every request uses TLS.
    #It also uses the default port of 5000.
    port = int(os.getenv("BLUEBUBBLES_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=tls_context())
