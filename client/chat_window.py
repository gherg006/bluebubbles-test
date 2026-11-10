import json
import hashlib
import os
import queue
import tempfile
import threading
import tkinter as tk
import time
import uuid
#All of our external libraries are imported here.
#More were added for optimisation that improve app performance.

from pathlib import Path
from tkinter import filedialog, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
#Here I am importing specific methods and attributes from libraries.

#This is where we are replacing openurl with our own method from the transport file.
from transport import open_server


#Colours chosen from the concept I made in the design stage.
BACKGROUND = "#d8e7fa"
PANEL = "#bdd9f1"
BUTTON = "#a8d4ed"
TEXT = "#1f3449"
WHITE = "#ffffff"

#These are some more constants that I have added to make my code easier to manage.
#It handles basic functionalities that I might want to change later down the line.
REFRESH_INTERVAL_MS = 1000
CONTACT_SORT_REFRESH_MS = 10000
UI_RESULT_INTERVAL_MS = 50
MESSAGE_PAGE_SIZE = 100
MESSAGE_BUBBLE_WIDTH = 390
#File size constraints that I am able to change.
MAXIMUM_FILE_BYTES = 2 * 1024 * 1024 * 1024
FILE_TRANSFER_TIMEOUT_SECONDS = 60 * 60


class MultipartFileBody:            #Needed for file uploads and transfers.
    #Stream one attachment so progress can advance while urllib sends it.
    #Needed for the new file system.
    #The chunk size is determined here and can be changed when needed.
    _chunk_size = 64 * 1024

    def __init__(self, recipient, filename, path, checksum, progress_callback):     #Sets up an instance for uploading files.
        #Generate a unique multipart boundary string with a random UUID.
        boundary = f"----BlueBubbles{uuid.uuid4().hex}"
        #Defines the content type for a multipart request.
        self.content_type = f"multipart/form-data; boundary={boundary}"
        #Creates the header portion of the request body.
        self._prefix = self._headers(boundary, recipient, filename, checksum)
        #Creates the trailer for the payload, being encoded in ascii.
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        #Open the file specified by the path in binary read mode.
        self._file = open(path, "rb")
        #Get the total file size in bytes from the FS.
        self._file_size = os.path.getsize(path)
        #Calculate combined length of header, trailer and file.
        self.content_length = len(self._prefix) + self._file_size + len(self._suffix)
        #Store the callback function that tracks the progress.
        self._progress_callback = progress_callback
        #Initialises the tracking counter for how many bytes have been sent already.
        self._sent_file_bytes = 0

    @staticmethod
    def _headers(boundary, recipient, filename, checksum):
        #Initialises a byte array.
        #This holds multipart form data headers.
        encoded = bytearray()
        #Loops through form fields for recipient, checksum.
        #Each is added to the payload every loop.
        for name, value in (("recipient", recipient), ("checksum", checksum)):
            #Append boundary seperator line in ASCII.
            encoded.extend(f"--{boundary}\r\n".encode("ascii"))
            #Applies content disposition header and value in utf-8 formatting.
            encoded.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        #Does the same for the last two file parts.
        encoded.extend(f"--{boundary}\r\n".encode("ascii"))
        encoded.extend(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
        )
        #Appends the content type header.
        #This indicates the binary stream data for the file.
        encoded.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        #Converts the bytearray to a byte object, then returns it.
        return bytes(encoded)

    def read(self, size=-1):                #Reads files to be sent.
        #If file size is null or negative, it switches to the predefined chunk size.
        if size is None or size < 0:
            size = self._chunk_size
        parts = []
        remaining = size
        #Loop until we have gathered enough data to process the full size
        while remaining:
            if self._prefix:
                #Process the header first.
                part, self._prefix = self._prefix[:remaining], self._prefix[remaining:]
            elif self._file is not None:
                #Read from the open file stream, capped by the chunk size or remaining bytes.
                part = self._file.read(min(remaining, self._chunk_size))
                if not part:
                    #Close and clear the file handle if EOF is reached.
                    self._file.close()
                    self._file = None
                    continue
                #Uodate the count of sent file bytes and trigger process callback.
                self._sent_file_bytes += len(part)
                self._progress_callback(self._sent_file_bytes, self._file_size)
            elif self._suffix:
                #Consume trailer of multipart file.
                part, self._suffix = self._suffix[:remaining], self._suffix[remaining:]
            else:
                break
            parts.append(part)
            remaining -= len(part)
        #Combine all parts of the loop if no data sources are available.
        return b"".join(parts)

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


class ChatWindow:
    #Show the main sharp-edged chat screen after login.
    def __init__(self, root, username, server_url):
        #Sets up the main chat.
        self.root = root
        self.username = username
        self.server_url = server_url
        self.users = []
        self.contacts = []
        #Added more variables that deals with contacts, sorting and refreshes.
        self.contact_details = {}
        self._displayed_contacts = []
        self._context_contact = None
        self._sort_mode = "saved"
        self._contacts_request = None
        self._contacts_refresh_pending = False
        self._read_sent = {}
        #Variables for messages
        self.message_text = tk.StringVar()
        self.send_status = tk.StringVar()
        self.search_text = tk.StringVar(value="Search")
        self.chat_title = tk.StringVar(value="Chat user")
        self.recipient = None
        self.current_messages = None
        #Variables for attachments.
        self.attachment = None
        self._has_older = False
        self._has_newer = False
        self._conversation_generation = 0
        self._history_request = None
        self._poll_request = None
        self._send_in_flight = False
        self._attachment_check = None
        self._download_in_flight = False
        self._contact_delete_in_flight = False
        self._ui_results = queue.Queue()

        #Sets the size and colours of the window.
        root.title("BlueBubbles")
        root.geometry("1080x650")
        root.minsize(980, 580)
        root.configure(bg=WHITE)
        #Calls all of the helper functions that are used to build the window, call users and refresh messages.
        #Contacts are now also loaded from the database so that they are saved every time the user opens the app.
        self._build_window()
        self._load_users()
        self._load_contacts()
        #New refresh and sorting added.
        self.root.after(UI_RESULT_INTERVAL_MS, self._drain_background)
        self._schedule_message_refresh()
        self.root.after(CONTACT_SORT_REFRESH_MS, self._refresh_contact_sort)

    def _build_window(self):
        #Keep the app box and separate sort box in the same arrangement as my design
        page = tk.Frame(self.root, bg=WHITE, padx=26, pady=20)
        page.pack(fill="both", expand=True)
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)

        #Adds all of the elements of the whole chat window using the tkinter grid.
        app = tk.Frame(page, bg=BACKGROUND, bd=1, relief="solid", padx=8, pady=8)
        app.grid(row=0, column=0, sticky="nsew")
        app.grid_columnconfigure(0, weight=4)
        app.grid_columnconfigure(1, weight=1)
        app.grid_rowconfigure(1, weight=1)
        self._build_header(app)
        self._build_chat(app)
        self._build_users(app)
        self._build_sorting(page)

    def _build_header(self, parent):
        #Add the logo area, exit button, chat title, and search.
        header = tk.Frame(parent, bg=BACKGROUND)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(2, weight=1)
        self._label(header, "BlueBubbles", 0, 0, width=14, font=("Arial", 10, "bold"), padx=8, pady=11)
        #Exit button that closes the app gracefully.
        tk.Button(header, text="Exit", command=self.root.destroy, bg="#f5aaa4", relief="solid", bd=1).grid(
            row=0, column=1, padx=(12, 8)
        )
        #Uses the same colour as the button but is a label that has the chat title.
        tk.Label(header, textvariable=self.chat_title, bg=BUTTON, fg=TEXT, relief="solid", bd=1).grid(
            row=0, column=2, sticky="ew", padx=(0, 8), ipady=5
        )
        #Creates the search box that you can use to search for users.
        search = tk.Entry(header, textvariable=self.search_text, bg=BUTTON, fg=TEXT, relief="solid", bd=1, width=17)
        search.grid(row=0, column=3, ipady=5)
        search.bind("<FocusIn>", self._clear_search_hint)
        self.search_text.trace_add("write", self._filter_users)

    def _build_chat(self, parent):
        #Builds the large left conversation panel and message entry bar.
        chat = tk.Frame(parent, bg=BACKGROUND)
        chat.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        chat.grid_columnconfigure(0, weight=1)
        chat.grid_rowconfigure(1, weight=1)
        #Displays what username you are logged in as.
        tk.Label(
            chat,
            text=f"Logged in as: {self.username}",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Arial", 9, "bold"),
        ).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        
        #The UI that loads the actual message box.
        message_area = tk.Frame(chat, bg=WHITE, bd=1, relief="solid")
        message_area.grid(row=1, column=0, sticky="nsew")
        message_area.grid_columnconfigure(0, weight=1)
        message_area.grid_rowconfigure(0, weight=1)
        self.message_canvas = tk.Canvas(message_area, bg=WHITE, highlightthickness=0)
        self.message_canvas.grid(row=0, column=0, sticky="nsew")
        
        #This adds a scroll bar to the message area.
        #Changed to our scroll method instead of trying to scroll directly.
        scrollbar = tk.Scrollbar(
            message_area,
            orient="vertical",
            command=self._scrollbar_scroll,         
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.message_canvas.configure(yscrollcommand=scrollbar.set)
        self.messages = tk.Frame(self.message_canvas, bg=WHITE)
        self.message_window = self.message_canvas.create_window(
            (0, 0), window=self.messages, anchor="nw"
        )
        #Adds all of the controls to the message area.
        self.messages.bind("<Configure>", self._update_message_scroll_region)
        self.message_canvas.bind("<Configure>", self._resize_message_frame)
        self.root.bind_all("<MouseWheel>", self._scroll_messages)
        self.root.bind_all("<Button-4>", self._scroll_messages)
        self.root.bind_all("<Button-5>", self._scroll_messages)
        self._show_empty_conversation()

        #The background and other keybinds are configured below.
        compose = tk.Frame(chat, bg=BACKGROUND, pady=8)
        compose.grid(row=2, column=0, sticky="ew")
        compose.grid_columnconfigure(0, weight=1)
        self.message_entry = tk.Entry(compose, textvariable=self.message_text, bg=WHITE, fg=TEXT, font=("Arial", 11), bd=1, relief="solid")
        self.message_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.message_entry.bind("<Return>", self._send_from_enter)
        self.message_entry.bind("<KP_Enter>", self._send_from_enter)
        
        #Button to send a message. It is placed next to the entry box.
        self.send_button = tk.Button(
            compose,
            text="Send",
            command=self.send_message,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 10, "bold"),
            bd=1,
            relief="solid",
            width=8,
        )
        #The send button grid position.
        self.send_button.grid(
            row=0, column=1, padx=(8, 0)
        )
        #The button to attach files in the application.
        self.attach_button = tk.Button(
            compose,
            text="Attach file",
            command=self.attach_file,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 10, "bold"),
            bd=1,
            relief="solid",
        )
        #The place it is set on the grid.
        self.attach_button.grid(row=0, column=2, padx=(8, 0))
        #The va\riables that store file progress data.
        self.attachment_status = tk.StringVar()
        self.attachment_progress = tk.DoubleVar(value=0)
        self.attachment_frame = tk.Frame(compose, bg=BACKGROUND)
        self.attachment_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        self.attachment_frame.grid_columnconfigure(1, weight=1)
        #Shows whether it has been sent or not.
        tk.Label(
            self.attachment_frame,
            textvariable=self.attachment_status,
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        #Shows the upload progress to the server.
        ttk.Progressbar(
            self.attachment_frame,
            maximum=100,
            variable=self.attachment_progress,
            mode="determinate",
        ).grid(row=0, column=1, sticky="ew")
        #The button to remove ann attachment.
        self.remove_attachment_button = tk.Button(
            self.attachment_frame,
            text="Remove",
            command=self._clear_attachment,
            bg="#f5aaa4",
            relief="solid",
            bd=1,
        )
        self.remove_attachment_button.grid(row=0, column=2, padx=(8, 0))
        self.attachment_frame.grid_remove()
        #The label shows whether the message was sent or not. This is the message status.
        tk.Label(
            compose,
            textvariable=self.send_status,
            bg=BACKGROUND,
            fg="#9a2d27",
            anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))

    def _build_users(self, parent):
        #Display only the accounts the user has chosen to message.
        users = tk.Frame(parent, bg=BACKGROUND)
        users.grid(row=1, column=1, sticky="nsew")
        users.grid_columnconfigure(0, weight=1)
        users.grid_rowconfigure(2, weight=1)
        #The button to add new users that they haven't contacted before.
        tk.Button(
            users,
            text="Add user",
            command=self._open_add_user_menu,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 9, "bold"),
            bd=1,
            relief="solid",
            pady=5,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(users, text="Messages", bg=PANEL, fg=TEXT, font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="ew")
        self.user_list = tk.Listbox(users, bg=WHITE, fg=TEXT, font=("Arial", 10), bd=1, relief="solid", selectbackground="#8bbde0", activestyle="none")
        self.user_list.grid(row=2, column=0, sticky="nsew")
        self.user_list.bind("<<ListboxSelect>>", self._select_user)

        #New options that come up when you right click.
        #Allows you to delete and pin users (Pin users TODO)
        self.user_list.bind("<Button-3>", self._open_contact_menu)
        self.contact_menu = tk.Menu(self.root, tearoff=0)
        self.contact_menu.add_command(label="Delete", command=self._delete_selected_contact)
        self.contact_menu.add_command(label="Pin (coming soon)", state="disabled")

    def _build_sorting(self, parent):
        # Keep the sorting actions in their own right-hand box.
        panel = tk.Frame(parent, bg=PANEL, bd=1, relief="solid", padx=8, pady=8)
        panel.grid(row=0, column=1, sticky="ns", padx=(24, 0))
        #Creates the buttons used to sort.
        self.sort_buttons = {}
        #Loops to create the buttons and links their names with the mode.
        for row, (label, mode) in enumerate((("Most Recent", "most_recent"), ("Alphabetical", "alphabetical"), ("Frequency", "frequency"), ("Date Added", "date_added"), ("New Messages", "new_messages"))):
            #The sorting method is used that sets a different style of sorting.
            command = lambda selected=mode: self._set_sort_mode(selected)
            #The same button that is duplicated in each run of the for loop.
            button = tk.Button(panel, text=label, command=command, bg=BUTTON, fg=TEXT, font=("Arial", 9), bd=1, relief="solid", width=16, pady=10)
            button.grid(
                row=row, column=0, pady=(0, 8), sticky="ew"
            )
            #The buttons are then sorted in order by their mode.
            self.sort_buttons[mode] = button

    def _label(self, parent, text, row, column, **options):
        #Create a square blue label.
        #Just a helper function so I don't need to keep repeating this code and so that labels are consistent everywhere.
        tk.Label(parent, text=text, bg=BUTTON, fg=TEXT, bd=1, relief="solid", **options).grid(row=row, column=column, sticky="ew", pady=(0, 8))

    def _post_ui(self, callback, *args):
        #Worker threads only queue data so Tk widgets stay on the UI thread.
        #Optimisation here.
        self._ui_results.put((callback, args))

    def _drain_background(self):
        for _ in range(100):
            #Run max 100 times to process background tasks
            try:
                callback, args = self._ui_results.get_nowait()
            except queue.Empty:
                #Stop if queue is empty.
                break
            #Executes callback function with its arguments on the UI thread.
            callback(*args)
        #Schedules the method to run again after a speficied amount of time.
        self.root.after(UI_RESULT_INTERVAL_MS, self._drain_background)

    def _run_background(self, work, on_complete):
        def run():
            try:
                #Execute the background workload function and assume there are no errors (initially).
                result = work()
                error = None
            except Exception as caught:
                #If an exception occues, catch it and set the result to none.
                #Throw an error as well.
                result, error = None, caught
            #Return completion callback with the result and error back to the UI
            self._post_ui(on_complete, result, error)
        #Start the workload in a new background thread.
        threading.Thread(target=run, daemon=True).start()

    def _load_users(self):
        #Ask the server for usernames used by the add-user menu.
        def fetch():
            #Open a connection to the server's user endpoint with a timeout.
            with open_server(f"{self.server_url}/users", timeout=5) as response:
                #Read, decode, parse json response, extract user list.
                return json.loads(response.read().decode("utf-8")).get("users", [])

        def loaded(users, error):
            #Update user list if successful, otherwise default to empty.
            self.users = users if error is None else []
            #Refreshes the user filters.
            self._filter_users()
        #Runs the fetch operation in the background and handles the results with loaded.
        self._run_background(fetch, loaded)

    def _load_contacts(self):
        #New method needed to retrieve saved contacts from the server.
        #Fetches the correct username to get the contacts for.
        if self._contacts_request is not None:
            #If a contact request is in progress, mark the refresh as pending.
            self._contacts_refresh_pending = True
            return
        #Creates a unique token for this speficic request.
        #Needed upon testing otherwise race conditions were created.
        request_token = object()
        self._contacts_request = request_token

        def fetch():
            #Opens a server connection to the contacts endoint with 5 second timeout
            with open_server(f"{self.server_url}/contacts", timeout=5) as response:
                #Read, decode and parse Json response.
                return json.loads(response.read().decode("utf-8"))

        def loaded(page, error):
            #Ignores result if newer contact result has taken place in the meantime.
            #AKA the old request is superseded by the new one.
            if self._contacts_request is not request_token:
                return
            #Clears the active contacts request.
            self._contacts_request = None
            if error is None:
                #Update contacts list and map contact details sorted by name from the response.
                self.contacts = page.get("contacts", [])
                self.contact_details = {
                    item["name"]: item for item in page.get("contact_details", [])
                }
                #Re-filters the users.
                self._filter_users()
            #If another refresh was requested while loading, trigger it now.
            #Prevents the contacts from refreshing mid request.
            if self._contacts_refresh_pending:
                self._contacts_refresh_pending = False
                self._load_contacts()
        #Executes the fetch operation in the background, also handling results with loaded.
        self._run_background(fetch, loaded)

    def _refresh_contact_sort(self):
        #If the sort mode relies on changing metrics, the contacts are reloaded periodically.
        #This includes frequency, new messages, as well as hhow recent it was.
        if self._sort_mode in ("most_recent", "frequency", "new_messages"):
            self._load_contacts()
        #Schedule this sort-refresh method to run again after a set time interval.
        self.root.after(CONTACT_SORT_REFRESH_MS, self._refresh_contact_sort)

    def _mark_read_through(self, contact, message_id):
        #Persist only messages actually reached in the open conversation.
        previous = self._read_sent.get(contact, 0)
        #If the message ID is <= previously recorded read state, it returns early.
        if message_id <= previous:
            return
        #Update the local read state tracker with the new message ID.
        self._read_sent[contact] = message_id
        #Constructs the network request to mark messages as read up to this ID on the server.
        #Like normal it is structured in json, and sent to the read endpoint, under contacts.
        request = Request(
            f"{self.server_url}/contacts/read",
            data=json.dumps({"contact": contact, "through_id": message_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        def mark():
            #Opens a server connection with a timeout and return whether it was a success or not.
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("success", False)

        def marked(success, error):
            #If an error occured/the server failed, roll back the load read reciepts.
            if error is not None or not success:
                if self._read_sent.get(contact) == message_id:
                    self._read_sent[contact] = previous
            else:
                #Otherwise, if it was a success, reload contacts to reflect the updated read state.
                self._load_contacts()
        #Execute the request in the background using the helper method.
        self._run_background(mark, marked)

    def _clear_search_hint(self, event):
        #Remove the search hint when the search text is cleared.
        if self.search_text.get() == "Search":
            self.search_text.set("")

    def _filter_users(self, *args):
        #Refresh the message list with saved contacts that match the search term.
        query = self.search_text.get().lower()
        matches = self.contacts if query == "search" else [user for user in self.contacts if query in user.lower()]
        #The if statements that are used to check what mode is selected.
        #Checks every mode individually.

        #Alphabetical sort.
        if self._sort_mode == "alphabetical":
            matches = sorted(matches, key=str.casefold)
        #Most recent sort.
        elif self._sort_mode == "most_recent":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("latest_message_id", 0), user.casefold()
            ))
        #Frequency sort (by message count and latest message).
        elif self._sort_mode == "frequency":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("message_count", 0),
                -self.contact_details.get(user, {}).get("latest_message_id", 0),
                user.casefold(),
            ))
        #Date added sort.
        elif self._sort_mode == "date_added":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("added_at", 0), user.casefold()
            ))
        #New messages (filters for unread messages and sorts in ascending order of message count)
        elif self._sort_mode == "new_messages":
            matches = [user for user in matches if self.contact_details.get(user, {}).get("unread_count", 0) > 0]
            matches = sorted(matches, key=lambda user: (
                -self.contact_details[user]["unread_count"],
                -self.contact_details[user].get("latest_message_id", 0),
                user.casefold(),
            ))
        #Updates the stored list of currently displayed contacts.
        self._displayed_contacts = list(matches)
        #Clear all UI items from the list.
        self.user_list.delete(0, "end")
        #Populate the list box with sorted users, as well as unread counts (if there are any).
        for user in matches:
            unread = self.contact_details.get(user, {}).get("unread_count", 0)
            self.user_list.insert("end", f"{user} ({unread})" if unread else user)
        #Maintain user selection if the active recipient is part of the filtered matches.
        if self.recipient in matches:
            self.user_list.selection_set(matches.index(self.recipient))

    def _set_sort_mode(self, mode):
        self._sort_mode = mode
        for button_mode, button in self.sort_buttons.items():
            button.configure(relief="sunken" if button_mode == mode else "solid")
        self._filter_users()
        if mode in ("most_recent", "frequency", "new_messages"):
            self._load_contacts()

    def _select_user(self, event):
        #Change the current chat label when a different user is selected.
        selected = self.user_list.curselection()
        #Checks whether the selection exists and the index is within the bounds of the displayed contacts.
        if selected and selected[0] < len(self._displayed_contacts):
            #Retrieve the contact menu.
            recipient = self._displayed_contacts[selected[0]]
            #Check the recipient is a different one from the current one.
            if recipient != self.recipient:
                #Opens the conversation window with the new contact.
                self._open_conversation(recipient)

    def _open_contact_menu(self, event):
        # Select the right-clicked contact before showing its actions.
        index = self.user_list.nearest(event.y)
        bounds = self.user_list.bbox(index)
        if (not bounds or index >= len(self._displayed_contacts)
                or not bounds[1] <= event.y <= bounds[1] + bounds[3]):
            return
        self._context_contact = self._displayed_contacts[index]
        self.user_list.selection_clear(0, "end")
        self.user_list.selection_set(index)
        try:
            self.contact_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.contact_menu.grab_release()

    def _delete_selected_contact(self):     #Deletes saved contacts from server then UI.
        #If a contact is already being deleted then don't do anything.
        if self._contact_delete_in_flight:
            return
        #Gets the index of the current selection (it would be highlighted in the program).
        selected = self.user_list.curselection()
        if self._context_contact is None and (not selected or selected[0] >= len(self._displayed_contacts)):
            return
        #Grabs the actual username from the contacts list to be put into the packet.
        contact = self._context_contact or self._displayed_contacts[selected[0]]
        self._context_contact = None
        #The data here has changed from username to contact.
        data = json.dumps({"contact": contact}).encode("utf-8")
        #This makes a request and points it to the contacts endpoint of the server.
        #It includes all of the parameters like the data, type (json here), as well as the deletion request.
        request = Request(
            f"{self.server_url}/contacts",
            data=data,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )

        #Flag that indicates a contact deletion is already in progress.
        self._contact_delete_in_flight = True

        def remove():
            #Open a server connection with a timeout for the deletion request.
            with open_server(request, timeout=5) as response:
                #Returns a success or failure status.
                return json.loads(response.read().decode("utf-8")).get("success", False)

        def removed(success, error):
            #Reset the contact deletion flag.
            self._contact_delete_in_flight = False
            #If an error occured or deletion failed, show an error message then exit.
            if error is not None or not success:
                self.send_status.set("Could not delete that contact.")
                return
            #Remove the contact label from the contacts list.
            if contact in self.contacts:
                self.contacts.remove(contact)
            #Remove contact details and refresh UI and contacts.
            self.contact_details.pop(contact, None)
            self._filter_users()
            self._load_contacts()
            #If the current active chat was deleted then reset the conversation view.
            if self.recipient == contact:
                self._conversation_generation += 1
                self.recipient = None
                self.current_messages = None
                self._history_request = None
                self._poll_request = None
                self.chat_title.set("Chat user")
                self._clear_messages()
                self._show_empty_conversation()
                self._scroll_to_latest()
        #Execute the deletion request in the background using the helper methods.
        self._run_background(remove, removed)

    def _open_add_user_menu(self):
        #Show registered accounts only when the user chooses to add a contact.
        menu = tk.Toplevel(self.root)
        menu.title("Add user")
        menu.resizable(False, False)
        menu.configure(bg=BACKGROUND, padx=12, pady=12)
        #Add a user to the conversation list.
        tk.Label(menu, text="Choose a user to message", bg=BACKGROUND, fg=TEXT).pack(anchor="w", pady=(0, 8))
        search_text = tk.StringVar()
        #Another user is actually added to the list on the side.
        tk.Entry(menu, textvariable=search_text, bg=WHITE, fg=TEXT, bd=1, relief="solid", width=26).pack(pady=(0, 8))
        user_picker = tk.Listbox(menu, bg=WHITE, fg=TEXT, bd=1, relief="solid", width=26, height=10)
        user_picker.pack()

        def show_matching_users(*args):
            #Show othe registered accounts that match the add-user search.
            query = search_text.get().lower()
            available = [
                user for user in self.users
                if user != self.username and user not in self.contacts and query in user.lower()
            ]
            #This clears the list and adds the new accounts.
            user_picker.delete(0, "end")
            for user in available:
                user_picker.insert("end", user)

        search_text.trace_add("write", show_matching_users)
        show_matching_users()

        def add_selected_user():
            #Add the selected account to the main messages list.
            selected = user_picker.curselection()
            if not selected:
                return
            user = user_picker.get(selected[0])
            #The server verifies that the contact is saved or not.
            #This ensures that you never get local only contacts and instead throws an error to the user.
            add_button.configure(state="disabled")

            #send_status is replaced by this method used to check if it is or not.
            def saved(success, error):
                #Check if menu window still exists.
                #If it does, nothing happens and it just returns.
                if not menu.winfo_exists():
                    return
                #If saving failed, re-enable the add button.
                #Also throw an error so that the user knows it failed.
                if error is not None or not success:
                    add_button.configure(state="normal")
                    self.send_status.set("Could not save that user to your chats.")
                    return

                #If successful, add the user to local contacts.
                self.contacts.append(user)
                #Then filter them, open the chat, and close the menu.
                self._filter_users()
                self._open_conversation(user)
                self._load_contacts()
                menu.destroy()
            #Does the contact saving operation in the background.
            self._run_background(lambda: self._save_contact(user), saved)

        #The button is then created and assigned to a variable, where it is then called to the correct position.
        add_button = tk.Button(menu, text="Add", command=add_selected_user, bg=BUTTON, fg=TEXT, bd=1, relief="solid", padx=18)
        add_button.pack(pady=(10, 0))

    def _save_contact(self, contact):
        #Save a contact that is loaded when the user logs in again.
        #The data request is being built to be sent to the server.
        #Also changed from "username" to "contact".
        data = json.dumps({"contact": contact}).encode("utf-8")
        #The request is built and sent to the contacts endpoint.
        request = Request(
            f"{self.server_url}/contacts",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        #Standard catch error handling, ensuring that the request arrives properly.
        try:
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("success", False)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return False

    def _open_conversation(self, recipient):
        #Load the selected account's saved conversation.
        #New variable to track the conversation generation number.
        self._conversation_generation += 1
        self.recipient = recipient
        self.current_messages = None

        #More new variables to do with sorting.
        self._has_older = False
        self._has_newer = False
        self._history_request = None
        self._poll_request = None
        
        self.send_status.set("")
        self.chat_title.set(f"Chat user: {recipient}")
        #All messages are cleared first.
        self._clear_messages()
        #Tells user messages are loading.
        self._add_message("BlueBubbles", "Loading messages...", "", True)
        self._load_page("latest")
        #The conversation is loaded and the chat is displayed straight away.

    def _fetch_conversation(self, recipient, before_id=None, after_id=None):
        #Retrieve the active conversation without changing the visible chat.
        #It gets the latest 100 rows.
        #Upon testing any more that that starts to take a large load on the server.
        #Especially apparent when more than one user is requesting at a time.

        #The parameters are constructed to ensure that the sorting is correct.
        parameters = {"with": recipient}
        if before_id is not None:
            parameters["before_id"] = before_id
        if after_id is not None:
            parameters["after_id"] = after_id
        
        #The subroutine tries to connect to the server and retrieve the conversation history.
        #It does this through the messages endpoint on the server.
        #Also changed from urlopen to my own open_server for maintainability.
        with open_server(f"{self.server_url}/messages?{urlencode(parameters)}", timeout=5) as response:
            page = json.loads(response.read().decode("utf-8"))
        #Messages are stored here temporarily.
        messages = page.get("messages")

        #Validates the server response matches the schema for messages.
        if (
            not isinstance(messages, list)      #Checks messages is not a list.
            or not isinstance(page.get("has_more"), bool)       #Checks if 'has_more' is not a boolean.
            or len(messages) > MESSAGE_PAGE_SIZE        #Checks if number of messages exceeds limit.
            or any(not isinstance(message.get("id"), int) for message in messages)      #Check if any messageID is missing
        ):
            #Raise an error if the server response format is invalid.
            raise ValueError("The server does not support bounded message pages.")
        #Returns the page of messages.
        return page

    def _load_page(self, direction):
        #Prevents concurrent history requests/loading when no contact is selected.
        if self._history_request is not None or not self.recipient:
            return
        #Ensure there are messages loaded if not loading the current aka latest messages.
        #This came up in testing where without this check, latest messages could be lost.
        if direction != "latest" and not self.current_messages:
            return
        recipient = self.recipient
        generation = self._conversation_generation
        cursor = None

        #Set the cursor based on whether its looking at older or newer messages.
        if direction == "older":
            cursor = self.current_messages[0]["id"]
        elif direction == "newer":
            cursor = self.current_messages[-1]["id"]

        #Creates a unique request token to mark history and progress.
        #I encountered an occasional race condition where one history would load before another.
        #Having this token ensures that they can be ordered correctly and the race condition doesn't happen.
        request_token = object()
        self._history_request = request_token
        self._poll_request = None

        def fetch():
            #Fetch conversation history depending on whether it is newest or oldest part of conversation.
            #This is based off of the cursor position (not actual cursor but where the user is scrolled to).
            return self._fetch_conversation(
                recipient,
                before_id=cursor if direction == "older" else None,
                after_id=cursor if direction == "newer" else None,
            )

        def loaded(page, error):        #New checks to ensure that the page was loaded properly.
            #Ignore response if newer request was made or convo generation changed.
            if self._history_request is not request_token or generation != self._conversation_generation:
                return
            #Clear the active history request flag.
            self._history_request = None
            #If an error occured then update the status bar with the appropriate message.
            if error is not None:
                self.send_status.set(
                    "Update the server before using message paging."
                    if isinstance(error, ValueError) else "Could not load messages."
                )
                return
            #Extract saved messages from the response page.
            saved_messages = page["messages"]
            #If no older messages, then update page flag and return.
            if direction == "older" and not saved_messages:
                self._has_older = False
                return
            #If no newer messages, update page flag and return.
            if direction == "newer" and not saved_messages:
                self._has_newer = False
                return
            #Updates current messages with newly loaded saved ones.
            self.current_messages = saved_messages
            #Update page state flags based on fetch direction and metadata.
            if direction == "latest":
                self._has_older, self._has_newer = page["has_more"], False
            elif direction == "older":
                self._has_older, self._has_newer = page["has_more"], True
            else:
                self._has_older, self._has_newer = True, page["has_more"]
            #Clear messages and render newly loaded ones.
            self._clear_messages()
            for message in saved_messages:
                self._add_saved_message(message)
            #If message list is empty, display an empty placeholder.
            if not saved_messages:
                self._add_message(self.recipient, "No messages yet.", "", True)
            #Scroll to the top when loading newer messages, otherwise scroll to latest.
            if direction == "newer":
                self._scroll_to_top()
            else:
                self._scroll_to_latest()
            #Automatically mark messages as read if you are viewing the latest ones.
            if saved_messages and not self._has_newer:
                self._mark_read_through(recipient, saved_messages[-1]["id"])
        #Execute this in the background so it doesn't affect other processes.
        self._run_background(fetch, loaded)

    def _append_new_messages(self, new_messages, scroll_to_latest=False):
        #Returns early if there are no new messages/current messages are not initialised.
        if not new_messages or self.current_messages is None:
            return
        #Check if the user is already scrolled to the bottom of the message window.
        was_at_latest = self.message_canvas.yview()[1] >= 0.99
        #Create a set of existing message IDs to see if there are duplicates.
        existing = {message["id"] for message in self.current_messages}
        #Filter new messages to include ones that are only not present.
        additions = [message for message in new_messages if message["id"] not in existing]
        #If there are no new additions, return early.
        if not additions:
            return
        #Sort the new messages by their ID.
        additions.sort(key=lambda message: message["id"])
        #Check if current incoming additions overlap or go before current messages.
        if self.current_messages and additions[0]["id"] < self.current_messages[-1]["id"]:
            #Merge them by using a dictionary with the key being the message ID.
            merged = {message["id"]: message for message in self.current_messages + additions}
            #If merge count exceeds max page size, then older messages can be requested.
            if len(merged) > MESSAGE_PAGE_SIZE:
                self._has_older = True
            self.current_messages = sorted(merged.values(), key=lambda message: message["id"])[-MESSAGE_PAGE_SIZE:]
            #Clear and re-render all messages.
            self._clear_messages()
            for message in self.current_messages:
                self._add_saved_message(message)
        else:
            #Standard appending of new messages at the end.
            if not self.current_messages:
                self._clear_messages()
            for message in additions:
                self._add_saved_message(message)
            self.current_messages.extend(additions)
            #Trim excess messages if the count exceeds the max page size limit.
            excess = len(self.current_messages) - MESSAGE_PAGE_SIZE
            if excess > 0:
                del self.current_messages[:excess]
                for child in self.messages.winfo_children()[:excess]:
                    child.destroy()
                self._has_older = True
        #Scroll to latest message message
        if scroll_to_latest or was_at_latest:
            self._scroll_to_latest()

    #Added this into a method to make it more maintainable for future features.
    #Was previously just in the refresh_conversation method.
    def _add_saved_message(self, message):
        #Gets all of the metadata and displays it in the chat window, doing this for each message.
        self._add_message(
            message["sender"],
            message["content"],
            message["time"],
            message["sender"] != self.username,
            message.get("date", ""),
            message.get("file_id"),
            message.get("filename"),
        )

    def _schedule_message_refresh(self):
        #Poll for only rows newer than the last displayed message.
        #Loads new rather than loading everything. Improved performance by triple in testing.
        #Also gives the app a smoother feeling, not constantly refreshing.
        self._poll_new_messages()       
        self.root.after(REFRESH_INTERVAL_MS, self._schedule_message_refresh)

    def _poll_new_messages(self):
        #Checks if polling should be skipped in case we don't need a refresh.
        #This saves on client resources and improves app responsiveness.
        if (
            not self.recipient or self.current_messages is None or self._has_newer
            or self._history_request is not None or self._poll_request is not None
            or self._send_in_flight
        ):
            return
        recipient = self.recipient
        generation = self._conversation_generation
        #Set the cursor ID t the latest message (or none if no messages).
        cursor = self.current_messages[-1]["id"] if self.current_messages else None
        #Creates a token to mark the poll request as in progress.
        #This prevents duplicates and race conditions.
        request_token = object()
        self._poll_request = request_token

        def fetch():
            #Fetch all messages from server that are after the cursor.
            return self._fetch_conversation(recipient, after_id=cursor)

        def loaded(page, error):
            #Ignore response if newer poll requests occured after the cursor.
            if self._poll_request is not request_token or generation != self._conversation_generation:
                return
            #Clear any active poll requests.
            self._poll_request = None
            #Handle errors by updating the status if a value error occurs.
            if error is not None:
                if isinstance(error, ValueError):
                    self.send_status.set("Update the server before using message paging.")
                return
            #I found here that when there was no cursor, the program would crash.
            #Tested and figured that there needs to be a way to prevent this.
            #Does a check now to ensure that a cursor is found, if it isn't then it returns nothing.
            if cursor is None:
                if not page["messages"] and not self.current_messages:
                    return
                self.current_messages = page["messages"]
                self._has_older = page["has_more"]
                self._clear_messages()
                for message in self.current_messages:
                    self._add_saved_message(message)
                if not self.current_messages:
                    self._add_message(self.recipient, "No messages yet.", "", True)
                else:
                    self._scroll_to_latest()
                    self._mark_read_through(recipient, self.current_messages[-1]["id"])
            else:
                #If a cursor exists, then check the scroll position and apply polled messages.
                was_at_latest = self.message_canvas.yview()[1] >= 0.99
                self._append_new_messages(page["messages"])
                if page["messages"]:
                    if was_at_latest: 
                        self._mark_read_through(recipient, self.current_messages[-1]["id"])
                        #If there is a dynamic sort, then reorder the messages.
                    if self._sort_mode in ("most_recent", "frequency", "new_messages"):
                        self._load_contacts()
            #The server will indicate if there are more messages available (continue polling).
            if page["has_more"] and cursor is not None:
                self._poll_new_messages()
        #Executes the fetch in the background as to not stall the application for the user.
        self._run_background(fetch, loaded)

    def _clear_messages(self):
        #Remove all currently displayed message rows.
        for child in self.messages.winfo_children():
            child.destroy()

    def _update_message_scroll_region(self, event):
        #Keep the scrollbar sized to the complete conversation.
        self.message_canvas.configure(scrollregion=self.message_canvas.bbox("all"))

    def _resize_message_frame(self, event):
        #Keep the message rows as wide as the visible conversation area.
        self.message_canvas.itemconfigure(self.message_window, width=event.width)

    def _scroll_messages(self, event):
        #Scroll only when the pointer is over the conversation, not other controls.
        widget = event.widget
        is_message_widget = widget == self.message_canvas
        #This while loop goes up the widget tree to see if the pointer is over a message row.
        while not is_message_widget and widget is not None:
            is_message_widget = widget == self.messages
            widget = widget.master
        if not is_message_widget:
            return
        if getattr(event, "delta", 0):
            #This if statement ensures that it works on mac and windows.
            amount = -int(event.delta / 120)
        else:
            #Since the server is running linux, I felt that it would make sense to do this for linux as well.
            amount = -1 if event.num == 4 else 1
        self.message_canvas.yview_scroll(amount, "units")
        self.root.after_idle(self._maybe_load_history)      #Added to check to load history or not.
        return "break"

    def _scrollbar_scroll(self, *args):
        #Updates the message vertical view based on the position of the scrollbar.
        self.message_canvas.yview(*args)
        self.root.after_idle(self._maybe_load_history)

    def _maybe_load_history(self):
        #Returns early if there are no loaded messages/if a request is already in progress.
        if not self.current_messages or self._history_request is not None:
            return
        #Get currently visible messages.
        top, bottom = self.message_canvas.yview()
        #Scroll checks to determine which messages to load.
        if top <= 0.001 and self._has_older:
            self._load_page("older")
        elif bottom >= 0.999 and self._has_newer:
            self._load_page("newer")
        elif bottom >= 0.999 and self.current_messages:
            self._mark_read_through(self.recipient, self.current_messages[-1]["id"])
        #If none of these checks pass, then load nothing.

    def _scroll_to_latest(self):
        #Show the most recent message after loading or sending a conversation.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(1)

    def _scroll_to_top(self):
        #Start a newly opened conversation at its earliest saved message.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(0)

    def _show_empty_conversation(self):
        #Explain why the chat area is empty before an account is selected.
        self._add_message("BlueBubbles", "Choose an account from the users list to start chatting.", "", True)

    def _add_message(self, sender, text, time, incoming, sent_date="", file_id=None, filename=None):
        #Added filename and fileID for files that are being sent.
        #Give each side a fixed-width column so message text starts consistently.
        block = tk.Frame(self.messages, bg=WHITE)
        block.pack(fill="x", padx=12, pady=(8, 0))
        bubble_color = WHITE
        bubble = tk.Frame(
            block, bg=bubble_color, bd=0, padx=10, pady=7,
        )
        #Checks whether incoming or outgoing.
        bubble.pack(side="left" if incoming else "right")
        bubble.grid_columnconfigure(0, minsize=MESSAGE_BUBBLE_WIDTH)

        header = tk.Frame(bubble, bg=bubble_color)
        header.grid(row=0, column=0, sticky="ew")
        #Bold label for sender's name.
        tk.Label(
            header, text=sender, bg=bubble_color, fg=TEXT,
            font=("Arial", 9, "bold"),
        ).pack(side="left")
        #Generates the timestamp by combining sent date and time values.
        timestamp = " ".join(value for value in (sent_date, time) if value)
        if timestamp:
            tk.Label(
                header, text=timestamp, bg=bubble_color, fg="#60788c",
                font=("Arial", 9),
            ).pack(side="right")
        #If the message contains a file attachment, treat it as interactive.
        #This allows the user to render it as an active download.
        if file_id and filename:
            tk.Button(
                bubble,
                text=filename,
                command=lambda: self._download_file(file_id, filename),
                bg=BUTTON,
                fg=TEXT,
                font=("Arial", 10, "underline"),
                bd=1,
                relief="solid",
                anchor="w",
                wraplength=MESSAGE_BUBBLE_WIDTH - 20,
            ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        #If not a file, then it is just a normal text message.
        else:
            #This was the add message system before the files were added.
            tk.Label(
                bubble, text=text, bg=bubble_color, fg="#334f66",
                font=("Arial", 10), justify="left", anchor="w",
                wraplength=MESSAGE_BUBBLE_WIDTH,
            ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        return block

    def send_message(self):
        #Save the new plain-text message and refresh the conversation.
        text = self.message_text.get().strip()
        #Checks if there is already data in flight.
        if self._send_in_flight or self._attachment_check is not None:
            return
        #Checks whether a recipient has been selected.
        if not self.recipient:
            self.send_status.set("Choose a user before sending a message.")
            return
        #Checks whether it is an attachment or not.
        if not text and self.attachment is None:
            self.send_status.set("Write a message or attach a file before sending.")
            self.message_entry.focus_set()
            return
        #Setting local variables and flags.
        recipient = self.recipient
        generation = self._conversation_generation
        attachment = self.attachment
        self._send_in_flight = True
        self._set_send_controls(False)
        pending = None
        #If text content is provided then clear messages.
        if text:
            if not self.current_messages:
                self._clear_messages()
            #Have the message sending text.
            pending = self._add_message(self.username, text, "Sending...", False)
            self._scroll_to_latest()
        if text:
            self.message_text.set("")
        self.send_status.set("Sending...")
        if attachment is not None:
            #Updates progress to 0 and sets the name of the upload to the file.
            self.attachment_progress.set(0)
            self.attachment_status.set(f"Uploading: {attachment['filename']} (0%)")

        def show_text_receipt(saved_message):
            #Callback handler triggered when finishes sending.
            if pending is not None and pending.winfo_exists():
                #Pending info is deleted.
                pending.destroy()
            if self.recipient == recipient and generation == self._conversation_generation:
                #Checks if there are any newer messages.
                #Uses this to essentially read new messages.
                if self.current_messages is None or self._has_newer:
                    self._history_request = None
                    self.current_messages = []
                    self._has_newer = False
                    self._append_new_messages([saved_message], scroll_to_latest=True)
                    self._load_page("latest")
                else:
                    self._append_new_messages([saved_message], scroll_to_latest=True)

        def work():
            #List to store any messages
            saved = []
            #Flags to track if the file or text has been sent.
            file_sent = False
            text_sent = False
            #If text is given then send the text.
            if text:
                response = self._send_text_message(recipient, text)
                if not response.get("success"):
                    #If it fails then return that it couldn't be sent.
                    return saved, file_sent, text_sent, response.get("message", "Message could not be sent.")
                text_sent = True
                if response.get("saved_message"):
                    saved.append(response["saved_message"])
                    #If there is also an attachment then update UI and receipt.
                    if attachment is not None:
                        self._post_ui(show_text_receipt, response["saved_message"])
            if attachment is not None:
                #Uploads and sends the attached file.
                response = self._send_attached_file(recipient, attachment)
                if not response.get("success"):
                    #Checks it passed and if not then it returns the error message.
                    return saved, file_sent, text_sent, response.get("message", "The file could not be uploaded.")
                file_sent = True
                if response.get("saved_message"):
                    saved.append(response["saved_message"])
                    #Returns the saved messages as well as if they were actually sent or not.
            return saved, file_sent, text_sent, ""

        def completed(result, error):
            #Checks nothing in progress first and enables send controls.
            self._send_in_flight = False
            self._set_send_controls(True)

            if pending is not None and pending.winfo_exists():
                pending.destroy()
            #If exception occurs, throw error message.
            if error is not None:
                result = ([], False, False, "Could not reach the server. Try again.")
            saved, file_sent, text_sent, message = result
            #If file and text were sent correctly, clear selection of file and text.
            if file_sent and self.attachment is attachment:
                self._clear_attachment()
            if text and not text_sent:
                self.message_text.set(text)
            #Verify user is still in the same chat.
            #Comes after issue that users would switch and be messaging someone,
            # yet the messages were sent to the chat before.
            #This check prevents that from happening.
            if self.recipient == recipient and generation == self._conversation_generation:
                if (self.current_messages is None or self._has_newer) and saved:
                    #Show the receipt now, then reconcile the latest page.
                    self._history_request = None
                    self.current_messages = []
                    self._has_newer = False
                    self._append_new_messages(saved, scroll_to_latest=True)
                    self._load_page("latest")
                else:
                    self._append_new_messages(saved, scroll_to_latest=True)
                if not saved and not self.current_messages:
                    self._clear_messages()
                    self._add_message(self.recipient, "No messages yet.", "", True)
                #Update the status bar.
                self.send_status.set(message)
                #Poll for incoming messages.
                self._poll_new_messages()
        #Execute this in the background registering completed when done.
        self._run_background(work, completed)

    def _send_text_message(self, recipient, text):
        #This was our original method of sending messages.
        #We can use the legacy method to still build requests.
        #File handling is done in another method though.
        #Also changed sender to recipient for contacts system.
        data = json.dumps(
            {"recipient": recipient, "content": text}
        ).encode("utf-8")
        request = Request(
            f"{self.server_url}/messages",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        #Exception handling used to catch any errors.
        #This is crucial to ensure that the program can handle errors gracefully.
        try:
            #Changed urlopen to open_server as well here.
            with open_server(request, timeout=5) as response:
                #Return the response instead of assuming it went through.
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except json.JSONDecodeError:
                return {"success": False, "message": "Message could not be sent."}
        except (URLError, TimeoutError, json.JSONDecodeError):
            return {"success": False, "message": "Could not reach the server. Try again."}
        #Error handling made more robust by returning the the json error, not just assuming.

    def attach_file(self):
        #Check the selected local file in the background; do not copy it.
        #Returns if there is no active chat contact selected.
        if not self.recipient:
            self.send_status.set("Choose a user before attaching a file.")
            return
        if self._send_in_flight or self._attachment_check is not None:
            return
        #Opens the file window to allow a user to select a local file.
        path = filedialog.askopenfilename(parent=self.root)
        if not path:
            return
        try:
            #Tries to get the file size
            file_size = os.path.getsize(path)
        except OSError:
            #If it can't then return an error.
            self.send_status.set("Could not read that file.")
            return
        if file_size > MAXIMUM_FILE_BYTES:
            #Ensures that the file is the correct size.
            self.send_status.set("Files cannot be larger than 2 GB.")
            return
        #Creates a unique token to mark the attachment as active.
        check_token = object()
        self._attachment_check = check_token
        #Stores current conversation.
        generation = self._conversation_generation
        #Disables send controls temporarily while message is sending/generating.
        self._set_send_controls(False)
        #Updates status label of the file and displays the widget in the UI.
        self.attachment_status.set(f"Checking: {os.path.basename(path)}")
        self.attachment_frame.grid()

        def checked(checksum, error):
            #Ignored if the response is outdated and token doesn't match.
            if self._attachment_check is not check_token:
                return
            #Clears active attachment flag and re-enables send controls
            self._attachment_check = None
            self._set_send_controls(True)
            #If conversation gen changed then clear the attachment
            if generation != self._conversation_generation:
                self._clear_attachment()
                return
            #If there is any error then clear the attachment and don't verify the file.
            if error is not None:
                self._clear_attachment()
                self.send_status.set("Could not verify that file.")
                return
            #Stores the file attachment.
            self.attachment = {
                "path": path,
                "filename": os.path.basename(path),
                "size": file_size,
                "checksum": checksum,
            }
            #Resets atttachment progress and updates the message status UI
            self.attachment_progress.set(0)
            self.attachment_status.set(
                f"Attached: {self.attachment['filename']} ({self._file_size_label(file_size)}) — press Send"
            )
            #Clear the status bar message.
            self.send_status.set("")

        #Runs the thread checksum on a thread in the background.
        self._run_background(lambda: self._file_checksum(path), checked)

    def _send_attached_file(self, recipient, attachment):
        #Runs a worker thread and progress passed results back to the UI.
        last_report = [0.0]
        
        #Method that reports the total sent already to the UI.
        def report(sent_bytes, total_bytes):
            now = time.monotonic()
            #Checks there is still progress to go.
            if sent_bytes < total_bytes and now - last_report[0] < 0.1:
                return
            last_report[0] = now
            #Returns to the UI the progress
            self._post_ui(self._update_attachment_progress, sent_bytes, total_bytes, attachment)

        try:
            #Constructs multipart file body request.
            #Uses metadata and progress callback.
            body = MultipartFileBody(
                recipient,
                attachment["filename"],
                attachment["path"],
                attachment["checksum"],
                report,
            )
        #Catches any errors and returns an unsuccessful dictionary message.
        except OSError:
            return {"success": False, "message": "Could not read the attached file. Attach it again."}
        try:
            #Builds the request to be sent to the files endpoint on the server.
            request = Request(
                f"{self.server_url}/files",
                data=body,
                headers={
                    "Content-Type": body.content_type,
                    "Content-Length": str(body.content_length),
                },
            )
            #Opens the server URL and posts the packet, with the request that was just created.
            #It also has a timeout so that the app is never hung.
            with open_server(request, timeout=FILE_TRANSFER_TIMEOUT_SECONDS) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        #Checks for any exceptions and handles them.
        except HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except json.JSONDecodeError:
                return {"success": False, "message": "The file could not be uploaded."}
        except (URLError, TimeoutError, json.JSONDecodeError):
            return {"success": False, "message": "Could not reach the server. Try again."}
        #Once done, the file is closed.
        finally:
            body.close()
        #A message is returned as success.
        if not response_body.get("success"):
            return response_body
        #If the checksum is the same on each side, it goes through.
        #Otherwise it fails and a message states that the checksum is different.
        if response_body.get("checksum") != attachment["checksum"]:
            return {"success": False, "message": "The server did not confirm the file checksum."}
        return response_body

    def _update_attachment_progress(self, sent_bytes, total_bytes, attachment):
        #Checks whether the attachment is there and if it is already in flight.
        if self.attachment is not attachment or not self._send_in_flight:
            return
        #Has the total percentage as 100.
        #Does a calculation to find current progress.
        percentage = 100 if not total_bytes else sent_bytes * 100 / total_bytes
        #Sets the progress as a percentage.
        self.attachment_progress.set(percentage)
        #Parses this and sends it to the UI to be rendered.
        self.attachment_status.set(
            f"Uploading: {attachment['filename']} ({percentage:.0f}%)"
        )

    def _clear_attachment(self):
        #Sets the attachment flag to none, then sets progress to zero.
        #It also sets the status to none and removes the element from UI.
        self.attachment = None
        self.attachment_progress.set(0)
        self.attachment_status.set("")
        self.attachment_frame.grid_remove()

    def _set_send_controls(self, enabled):      #Toggles send controls on and off.
        #They are always on unless turned off.
        state = "normal" if enabled else "disabled"

        #When there is a message, send, attach or remove, the controls are checked.
        self.message_entry.configure(state=state)
        self.send_button.configure(state=state)
        self.attach_button.configure(state=state)
        self.remove_attachment_button.configure(state=state)

    @staticmethod
    def _file_size_label(size):
        #Format a given file size in bytes to a readable string.
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    @staticmethod
    def _file_checksum(path):
        #Computes and returns the sha-256 checksum digest.
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _download_file(self, file_id, filename):
        #Received attachments stay on the server until this button is clicked.
        #Prevents concurrent downloads.
        if self._download_in_flight:
            return
        #Open a save file dialog to let user select where to save the file
        target = filedialog.asksaveasfilename(parent=self.root, initialfile=filename)
        if not target:
            return
        #Mark download as active and update status.
        self._download_in_flight = True
        self.send_status.set(f"Downloading {filename}...")
        #Mark download as finished, resetting download flag.
        def finished(_result, error):
            self._download_in_flight = False
            self.send_status.set(
                "The file could not be downloaded or verified." if error is not None else ""
            )
        #Runs all on a background thread.
        self._run_background(lambda: self._download_to_path(file_id, target), finished)

    def _download_to_path(self, file_id, target):
        #Construct HTTPS request to fetch the file by its ID.
        request = Request(f"{self.server_url}/files/{quote(file_id)}")
        temporary_path = None
        try:
            #Open a connection to the server with a file transfer timeout.
            with open_server(request, timeout=FILE_TRANSFER_TIMEOUT_SECONDS) as response:
                #Extract and normalise the checksum from the response header.
                checksum = response.headers.get("X-Content-SHA256", "").lower()
                #Validate that it is a 64 character hex string.
                if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
                    raise OSError("The server did not provide a valid file checksum.")
                #Creates a temp file in the same directory as the target.
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=".bluebubbles-download-", suffix=".tmp", dir=Path(target).parent
                )
                digest = hashlib.sha256()
                #Open the temporary file descriptor.
                with os.fdopen(descriptor, "wb") as downloaded_file:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        downloaded_file.write(chunk)
                #Verify that the download file's computed hash matches the server's checksum.
                if digest.hexdigest() != checksum:
                    raise OSError("Downloaded file checksum does not match.")
            #Ensure the temporary file cleanup happens if an error occurs mid download.
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            #Ensure file cleanup happens if the full file doesn't work.
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def _send_from_enter(self, event):
        #Send the message when Enter is pressed in the message field.
        self.send_message()
        return "break"
