#HTTP server handling authentication.

import hashlib
import os
import subprocess

from flask import Flask, jsonify, request

from message_system import MessageSystem

#I used the same style as before where I seperate all of the external libraries from the internal ones.
#This makes it easier to understand where different methods and classes are coming from.

class ServerAuth:
    #Checks and creates postgres table.
    #Server authentication is handled by this class and the MessageSystem class.

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
        #Hashes passwords to store in the DB.
        #Hashes are one way so the original password cannot be retrieved from the hash.
        return hashlib.sha256(password.encode("utf-8")).hexdigest()
        #This encodes it in UTF-8, then hashes it using sha256, and returns the hash as a hex string.

    def _run_query(self, query, values):
        #Runs a query through the postgres client.
        command = [
            "psql", "-X", "-q", "-t", "-A", "-h", self.host,
            "-U", self.user, "-d", self.database, "-v", "ON_ERROR_STOP=1",
        ]
        #These are the flags that can be used that are within the list.
        #The other things are the host, user and database that are being used for the query.
        for name, value in values.items():
            command.extend(["-v", f"{name}={value}"])   #This adds values to the dictionary that can be used in the query.
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
        #This runs the actual query then returns the result, whether that is an output, or a boolean outcome
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
        exists = self._run_query(
            "SELECT 1 FROM users WHERE username = :'username' LIMIT 1;", values
        )
        #Checks if the username already exists
        if exists.returncode != 0:
            return False, "The server could not save the account."
            #If there is an error with the DB, it will return false and a message on the login screen.
        if exists.stdout.strip() == "1":
            return False, "That username is already in use."
            #If the username is in use, it will return false too, as well as a message on the login screen.

        saved = self._run_query(
            "INSERT INTO users (username, password_hash) "
            "VALUES (:'username', :'password_hash');", values
        )
        #Takes the user's username and password, hashes it, then saves it to the DB
        if saved.returncode != 0:
            return False, "The server could not save the account."
        return True, "Account created."
        #This is then a final check to ensure that the account was actually saved in the DB.
        #This ensures that accounts are actually created and not just assumed to be created.

    def users(self):
        #Return every registered username for the client user list.
        result = self._run_query("SELECT username FROM users ORDER BY username;", {})
        #The SQL statement is integrated into the code then passed onto the DB to be run.
        #The result is then written to a private variable,
        if result.returncode != 0:
            return []
        return [name for name in result.stdout.splitlines() if name]
        #The code is repeated until there are no more results that the server gives back.
        #This whole list is then returned to the client to be displayed in the user list on the chat window.


app = Flask(__name__)           #The flask application is initialised.
auth = ServerAuth()             
messages = MessageSystem(auth._run_query)
#The server authentication and message classes are initialised.
#The message system passes the DB query method from the auth class, used for saving and retrieving messages.


def _credentials():             #Helper function to read the username and password from a request.
    #Read the two fields in the json string.
    data = request.get_json(silent=True) or {}
    return data.get("username", "").strip(), data.get("password", "")
    #Returns the username and password as a tuple, as well as stripping any whitespace from the string.


@app.post("/login")             #Login endpoint for the client to send a request to.
def login():
    #Handles login request from client.
    username, password = _credentials()
    #Checks if the username and password are empty, and returns an error message if they are.
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    #If not empty, it checks the credentials against the DB.
    if auth.login(username, password):
        return jsonify(success=True, message="Logged in.")
    return jsonify(success=False, message="Incorrect username or password."), 401
    #It does all the server side checks that are possible, before sending the result back to the client.
    #This helps prevent errors showing on the client side, as well as preventing malicious requests from affecting the server.


@app.post("/register")          #Registration endpoint for the client to send a request to.
def register():
    #Handles registration request from client.
    username, password = _credentials()
    #This does the same empty checks as the login endpoint, returning a message if the fields are empty.
    #It parses it to json then checks it against the DB
    if not username or not password:
        return jsonify(success=False, message="Enter a username and password."), 400
    #Only when the creds are not empty, and the username is not in use, then it will return a success.
    #This then triggers a client message to say that the account has been created, and they can log in.
    success, message = auth.register(username, password)
    return jsonify(success=success, message=message), 201 if success else 409
    #This is the final return check to ensure that the account was created.


@app.get("/users")              #Users list endpoint for the client to send a request to.
def users():
    #Return the registered usernames used by the client users list using a parsed json string.
    return jsonify(users=auth.users())


@app.get("/messages")               #Messages endpoint for the client to send a request to.
def get_messages():
    #Return the selected conversation for the logged-in user.
    username = request.args.get("username", "").strip()
    other_user = request.args.get("with", "").strip()
    #Gets both usernames that are being used in the conversation, and checks if they are empty.
    if not username or not other_user:
        return jsonify(messages=[]), 400
        #If either usernames are empty it will return an error and an empty list as to not give an unexpected result to the client.
    return jsonify(messages=messages.conversation(username, other_user))
        #If everything goes to plan, then the messages are then returned to the client in a json format.
        #The client will then parse them and display them in the chat window.


@app.post("/messages")      #The same message endpoint, yet this is for sending messages to the server rather than retrieving them.
def send_message():
    #Save a plain text message for another account.
    data = request.get_json(silent=True) or {}
    sender = data.get("sender", "").strip()
    recipient = data.get("recipient", "").strip()
    content = data.get("content", "").strip()
    #The local variables are set to the values that are sent from the client.
    #They are also stripped of whitespace to avoid any unexpected results from the server.
    if not sender or not recipient or not content:
        return jsonify(success=False, message="Enter a recipient and message."), 400
    if messages.send(sender, recipient, content):
        return jsonify(success=True, message="Message sent."), 201
    return jsonify(success=False, message="The message could not be sent."), 400
    #All of the checks are done on the server side to ensure that the message is actually sent.
    #If any of the checks are not met, then the server will return an error message that is displayed in the chat window.

if __name__ == "__main__":
    #Listen on the server's network address so a client machine can connect.
    app.run(host="0.0.0.0", port=5000, debug=False)
