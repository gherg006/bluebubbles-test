#HTTP server handling authentication

import hashlib
import os
import subprocess

from flask import Flask, jsonify, request

from message_system import MessageSystem
from tls import tls_context

#Same as usual, I have seperated internal and external library imports.
class ServerAuth:
    #Checks and creates postgres table (if not already present).

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
        #hashes passwords
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def _run_query(self, query, values):            #Runs a query through the postgres client.
        
        #These are the flags that can be used that are within the list.
        #The other things are the host, user and database that are being used for the query.
        command = [
            "psql", "-X", "-q", "-t", "-A", "-h", self.host,
            "-U", self.user, "-d", self.database, "-v", "ON_ERROR_STOP=1",
        ]

        for name, value in values.items():
            command.extend(["-v", f"{name}={value}"])       #This adds values to the dictionary that can be used in the query.
        environment = os.environ.copy()
        environment["PGPASSWORD"] = self.password
        #Copies in the environment variables and injects the password securely into PGPASSWORD.
        return subprocess.run(
            command,
            input=query,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        #This runs the actual query then returns the result, whether that is an output, or a boolean outcome.
        #It is passed in as a simple string, and has a 10 second timeout to avoid hanging if it takes too long.

    def login(self, username, password):
        #Returns true when both creds match.
        result = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' "
            "AND password_hash = :'password_hash' LIMIT 1;",
            {"username": username, "password_hash": self._hash_password(password)},
        )
        #Takes the creds that are inputted and checks them against the database.
        return result.returncode == 0 and result.stdout.strip() == "1"
        #Checks what the database returned and returns true if the creds match, otherwise false.

    def register(self, username, password):
        #Saves new user unless that username is already in use.
        values = {"username": username, "password_hash": self._hash_password(password)}
        #Checks if the username already exists
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

    def users(self):         #Return every registered username for the client user list.
        #SQL statement is run then written to result.
        result = self._run_query("SELECT username FROM users ORDER BY username;", {})
        #Validation added here to ensure something was actually returned.
        if result.returncode != 0:
            return []
        return [name for name in result.stdout.splitlines() if name]

    def create_contacts_table(self):            #Create the per-user contact list used to restore chat contacts.
        #Query is build to build the table.
        #It includes userid, contactid, timestamp and primary key.
        result = self._run_query(
            "CREATE TABLE IF NOT EXISTS chat_contacts ("
            "\"userID\" INTEGER NOT NULL REFERENCES users(\"userID\") ON DELETE CASCADE, "
            "contact_id INTEGER NOT NULL REFERENCES users(\"userID\") ON DELETE CASCADE, "
            "added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (\"userID\", contact_id), "
            "CHECK (\"userID\" <> contact_id)"
            ");",
            {},
        )
        #Then returns whether one was created or not.
        return result.returncode == 0

    def contacts(self, username):           #Gets all of a selected contacts.
        #I took out the contacts from the users as now they are in different databases.
        #Runs the query for the table to get all the contact relations.
        result = self._run_query(
            "SELECT contact.username FROM chat_contacts saved "
            "JOIN users owner ON owner.\"userID\" = saved.\"userID\" "
            "JOIN users contact ON contact.\"userID\" = saved.contact_id "
            "WHERE owner.username = :'username' "
            "ORDER BY saved.added_at, contact.username;",
            {"username": username},
        )
        #Checks whether the query was successful.
        if result.returncode != 0:
            return []
        #Returns the output line by line, filtering out any empty strings.
        #This ends up as a clean list of usernames as contacts for one user.
        return [name for name in result.stdout.splitlines() if name]

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


app = Flask(__name__)                           #Creates an instance of flask web app.
auth = ServerAuth()                             #Creates an instance of the server authentication class.
auth.create_contacts_table()                    #Sets up the database if it isn't already present.
messages = MessageSystem(auth._run_query)       #Initialises the message system.


def _credentials():             #Helper function to read the username and password from a request.
    #Read the two fields in the json string.
    data = request.get_json(silent=True) or {}
    #Returns the username and password as a tuple, as well as stripping any whitespace from the string.
    return data.get("username", "").strip(), data.get("password", "")


@app.post("/login")             #Login endpoint for the client to send a request to.
def login():                    #Handles login request from client
    username, password = _credentials()
    #Checks if the username and password are empty, and returns an error message if they are.
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    #Checks the username and password against the server.
    if auth.login(username, password):
        return jsonify(success=True, message="Logged in.")
    return jsonify(success=False, message="Incorrect username or password."), 401
    #It does all the server side checks that are possible, before sending the result back to the client.
    #This helps prevent errors showing on the client side, as well as preventing malicious requests from affecting the server.


@app.post("/register")          #Register endpoint for the client to send a request to.
def register():                 #Handles registration request from client.
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


@app.get("/users")              #Users list endpoint for the client to send a request to.
def users():
    #Return the registered usernames used by the client users list using a parsed json string.
    return jsonify(users=auth.users())


@app.get("/contacts")           #Contacts endpoint for clients to send requests to.
def get_contacts():
    #Return the contacts that should remain in this account's sidebar.
    username = request.args.get("username", "").strip()
    #Checks if the username exists and if there are no contacts.
    if not username:
        return jsonify(contacts=[]), 400
    #Returns the contacts list in a json string.
    return jsonify(contacts=auth.contacts(username))


@app.post("/contacts")
def add_contact():
    #Save an account to the caller's persistent sidebar in the DB.
    data = request.get_json(silent=True) or {}
    #Username and contact are passed in.
    username = data.get("username", "").strip()
    contact = data.get("contact", "").strip()
    #Does checks to ensure contact isn't repeated.
    #If everything suceeds, the contact is saved to the DB.
    if not username or not contact or username == contact:
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
    username = data.get("username", "").strip()
    contact = data.get("contact", "").strip()
    if not username or not contact or username == contact:
        return jsonify(success=False, message="Choose a contact to delete."), 400
    if auth.delete_contact(username, contact):
        #Returns a deleted message if everything goes correctly.
        return jsonify(success=True, message="Contact deleted.")
    #Catches all exceptions not listed above
    return jsonify(success=False, message="That contact could not be deleted."), 404


@app.get("/messages")               #The messages endpoint a client can send a request to.
def get_messages():
    #Return the selected conversation for the logged-in user.
    username = request.args.get("username", "").strip()
    other_user = request.args.get("with", "").strip()
    #Puts all the messages in a list.
    if not username or not other_user:
        return jsonify(messages=[]), 400
    return jsonify(messages=messages.conversation(username, other_user))
    #Finally confirms that it all was all retrieved.


@app.post("/messages")
def send_message():
    #Save an encrypted message for another registered account.
    #The fields are saved to local variables.
    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "").strip()
    recipient = data.get("recipient", "").strip()
    content = data.get("content", "").strip()
    #Checks are done to ensure there is data to be sent
    #Verification is then also done as to whether messages were successful or not.
    if not sender or not recipient or not content:
        return jsonify(success=False, message="Enter a recipient and message."), 400
    if messages.send(sender, recipient, content):
        return jsonify(success=True, message="Message sent."), 201
    #There is a catchall just in case all of the checks fail.
    return jsonify(success=False, message="The message could not be sent."), 400


if __name__ == "__main__":
    #The API intentionally has no plaintext HTTP mode so every request needs to use TLS
    port = int(os.getenv("BLUEBUBBLES_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, ssl_context=tls_context())
