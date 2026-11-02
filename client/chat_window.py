import json
import tkinter as tk
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

#This is where we are replacing openurl with our own method from the transport file.
from transport import open_server

#For this file, it is just external dependencies, mostly to do with UI as well as URL handling.

#Colours chosen from the concept I made in the design stage.
BACKGROUND = "#d8e7fa"
PANEL = "#bdd9f1"
BUTTON = "#a8d4ed"
TEXT = "#1f3449"
WHITE = "#ffffff"
REFRESH_INTERVAL_MS = 1000


class ChatWindow:
    #Show the main chat screen after login.
    def __init__(self, root, username, server_url):
        #Sets up the main chat.
        self.root = root
        self.username = username
        self.server_url = server_url
        self.users = []
        self.contacts = []
        self.message_text = tk.StringVar()
        self.send_status = tk.StringVar()
        self.search_text = tk.StringVar(value="Search")
        self.chat_title = tk.StringVar(value="Chat user")
        self.recipient = None
        self.current_messages = None

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
        self._schedule_message_refresh()

    def _build_window(self):
        #Keep the app box and separate sort box in the same arrangement as my design.
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
        scrollbar = tk.Scrollbar(
            message_area,
            orient="vertical",
            command=self.message_canvas.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.message_canvas.configure(yscrollcommand=scrollbar.set)
        self.messages = tk.Frame(self.message_canvas, bg=WHITE)
        self.message_window = self.message_canvas.create_window(
            (0, 0), window=self.messages, anchor="nw"
        )

        #Adds all of the controls to the message area.
        self.messages.grid_columnconfigure(0, weight=1)
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
        tk.Button(
            compose,
            text="Send",
            command=self.send_message,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 10, "bold"),
            bd=1,
            relief="solid",
            width=8,
        ).grid(
            row=0, column=1, padx=(8, 0)
        )

        #The label shows whether the message was sent or not. This is the message status.
        tk.Label(
            compose,
            textvariable=self.send_status,
            bg=BACKGROUND,
            fg="#9a2d27",
            anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

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
        #The box that messages show up in.
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
        #Keep the sorting actions in their own right-hand box.
        panel = tk.Frame(parent, bg=PANEL, bd=1, relief="solid", padx=8, pady=8)
        panel.grid(row=0, column=1, sticky="ns", padx=(24, 0))
        #Create the buttons for the sorting actions.
        #Sorting not fully configured yet, only alphabetical for now
        #TODO other sorting methods.
        for row, (label, command) in enumerate((("Most Recent", self._keep_order), ("Alphabetical", self._sort_alphabetical), ("Frequency", self._keep_order), ("Date Added", self._keep_order), ("New Messages", self._keep_order))):
            tk.Button(panel, text=label, command=command, bg=BUTTON, fg=TEXT, font=("Arial", 9), bd=1, relief="solid", width=16, pady=10).grid(
                row=row, column=0, pady=(0, 8), sticky="ew"
            )

    def _label(self, parent, text, row, column, **options):
        #Create a square blue label.
        #Just a helper function so I don't need to keep repeating this code and so that labels are consistent everywhere.
        tk.Label(parent, text=text, bg=BUTTON, fg=TEXT, bd=1, relief="solid", **options).grid(row=row, column=column, sticky="ew", pady=(0, 8))

    def _load_users(self):
        #Ask the server for usernames (used by the add user menu).
        #Also changed from using URLopen to open_server, makes the code more maintainable (using a method rather than from the raw library).
        try:
            with open_server(f"{self.server_url}/users", timeout=5) as response:
                self.users = json.loads(response.read().decode("utf-8")).get("users", [])
                #The client sends it to the endpoint to update the list of users.
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            self.users = []
        self._filter_users()
        #The return from the server is appended to the list of users.
        #It is then filtered through based on the selected filter.

    def _load_contacts(self):           #New method needed to retrieve saved contacts from the server
        #Fetches the correct username to get the contacts for.
        parameters = urlencode({"username": self.username})
        #Opens the URL on the contacts endpoint, with a 5s timeout.
        #Then decodes the response and passes it off to the UI
        try:
            with open_server(f"{self.server_url}/contacts?{parameters}", timeout=5) as response:
                self.contacts = json.loads(response.read().decode("utf-8")).get("contacts", [])
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            #In the case of an error, it returns an empty list to indicate this.
            self.contacts = []
            #TODO make an error message as well.
        #It then applies the current filter to those users that the server returned.
        self._filter_users()

    def _clear_search_hint(self, event):
        #Remove the search hint when the search text is cleared.
        if self.search_text.get() == "Search":
            self.search_text.set("")

    def _filter_users(self, *args):
        #Refresh the message list with saved contacts that match the search term.
        query = self.search_text.get().lower()
        matches = self.contacts if query == "search" else [user for user in self.contacts if query in user.lower()]
        self.user_list.delete(0, "end")
        for user in matches:
            self.user_list.insert("end", user)

    def _sort_alphabetical(self):
        #Sort the selected contacts alphabetically.
        self.contacts.sort(key=str.lower)
        self._filter_users()

    def _keep_order(self):
        #Preserve database order until messages are actually stored by the server.
        self._filter_users()

    def _select_user(self, event):
        #Change the current chat label when a different user is selected.
        selected = self.user_list.curselection()
        if selected:
            self._open_conversation(self.user_list.get(selected[0]))


    def _open_contact_menu(self, event):            #Open the action menu for each contact (shows delete or pin).
        #Select the right-clicked contact before showing its actions.
        index = self.user_list.nearest(event.y)
        bounds = self.user_list.bbox(index)
        #Ensures the cursor is actually on that contact.
        if not bounds or not bounds[1] <= event.y <= bounds[1] + bounds[3]:
            return
        self.user_list.selection_clear(0, "end")
        self.user_list.selection_set(index)
        #This opens up the action menu.
        try:
            self.contact_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.contact_menu.grab_release()

    def _delete_selected_contact(self):             #Deletes saved contacts from server the UI.
        #Gets the index of the current selection (it would be highlighted in the program).
        selected = self.user_list.curselection()
        #If nothing is selected, the function stops to prevent errors.
        if not selected:
            return
        #This grabs the actual username string from the selected contacts list.
        contact = self.user_list.get(selected[0])
        #This creates the payload in json.
        data = json.dumps({"username": self.username, "contact": contact}).encode("utf-8")
        #This makes a request and points it to the contacts endpoint of the server.
        #It includes all of the parameters like the data, type (json here), as well as the deletion request.
        request = Request(
            f"{self.server_url}/contacts",
            data=data,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        #Standard exception handling to ensure that the request has gone through.
        try:
            with open_server(request, timeout=5) as response:
                #Parses the json to ensure that it was successful.
                success = json.loads(response.read().decode("utf-8")).get("success", False)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            #General catch errors.
            success = False
        #If it didn't succeed, it will tell the user that the contact couldn't be deleted.
        if not success:
            self.send_status.set("Could not delete that contact.")
            return
        #Removes the actual contact from the application UI.
        self.contacts.remove(contact)
        #Refreshes the filter in case the deletion changed the order.
        self._filter_users()
        #If the current chat was the deleted user's, it will clear it and put the placeholder in.
        if self.recipient == contact:
            self.recipient = None
            self.current_messages = None
            self.chat_title.set("Chat user")
            self._clear_messages()
            self._show_empty_conversation()
            self._scroll_to_latest()

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
            #Show only the accounts that match the add-user search.
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
            if not self._save_contact(user):
                self.send_status.set("Could not save that user to your chats.")
                return
            self.contacts.append(user)
            self._filter_users()
            self._open_conversation(user)
            menu.destroy()

        tk.Button(menu, text="Add", command=add_selected_user, bg=BUTTON, fg=TEXT, bd=1, relief="solid", padx=18).pack(pady=(10, 0))

    def _save_contact(self, contact):
        #Save a contact that is loaded when the user logs in again.
        #The data request is being built to be sent to the server.
        data = json.dumps({"username": self.username, "contact": contact}).encode("utf-8")
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
        self.recipient = recipient
        self.current_messages = None
        self.send_status.set("")
        self.chat_title.set(f"Chat user: {recipient}")
        self._refresh_conversation(scroll_to_top=True)
        #The conversation is loaded and the chat is displayed straight away.

    def _fetch_conversation(self):
        #Retrieve the active conversation without changing the visible chat.
        parameters = urlencode({"username": self.username, "with": self.recipient})
        #The subroutine tries to connect to the server and retrieve the conversation history.
        #It does this through the messages endpoint on the server.
        #Also changed from urlopen to my own open_server for maintainability.
        try:
            with open_server(f"{self.server_url}/messages?{parameters}", timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("messages", [])
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return None
        #This returns the json messages, and if there are any errors it returns nothing.

    def _refresh_conversation(self, scroll_to_top=False, scroll_to_latest=False):
        #Redraw only when polling finds a new or changed message.
        saved_messages = self._fetch_conversation()
        if saved_messages is None or saved_messages == self.current_messages:
            return
        was_at_latest = self.message_canvas.yview()[1] >= 0.99
        self._clear_messages()
        #This fetches the conversation history from the server.
        #It also gets all of the metadata associated with the message.
        for message in saved_messages:
            self._add_message(
                message["sender"],
                message["content"],
                message["time"],
                message["sender"] != self.username,
                message.get("date", ""),            #Date also added here as a new feature
            )
        #This redraws the messages.
        if not saved_messages:
            self._add_message(self.recipient, "No messages yet.", "", True)
        self.current_messages = saved_messages
        #Below fixes the old scroll issue where it would always leave a blank space at the top.
        if scroll_to_top:
            self._scroll_to_top()
        elif scroll_to_latest or was_at_latest:
            self._scroll_to_latest()
        #At the end, it scrolls to the bottom, being the latest message that is loaded.

    def _schedule_message_refresh(self):
        #Check the open conversation regularly so another device's messages appear.
        if self.recipient:
            self._refresh_conversation()
        self.root.after(REFRESH_INTERVAL_MS, self._schedule_message_refresh)

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
        return "break"
        #Finally returning the break string stops double scrolling or any other artefacts.

    def _scroll_to_latest(self):
        #Show the most recent message after loading or sending a conversation
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(1)

    def _scroll_to_top(self):
        #Start a newly opened conversation at its earliest saved message.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(0)

    def _show_empty_conversation(self):
        #Explain why the chat area is empty before an account is selected.
        self._add_message("BlueBubbles", "Choose an account from the users list to start chatting.", "", True)

    def _add_message(self, sender, text, time, incoming, sent_date=""):
        #Add a plain message line to the conversation area.
        row = self.messages.grid_size()[1]
        #The message box is added where the message itself is added into the box.
        block = tk.Frame(self.messages, bg=WHITE)
        block.grid(row=row, column=0, sticky="w" if incoming else "e", padx=10, pady=(10, 0))
        #The new timestamp feature is below and needs to be displayed.
        #Time in the message label was also replaced by the actual timestamp.
        timestamp = " ".join(value for value in (sent_date, time) if value)
        tk.Label(block, text=f"{sender}    {timestamp}".strip(), bg=WHITE, fg=TEXT, font=("Arial", 9, "bold")).pack(anchor="w")
        tk.Label(block, text=text, bg=WHITE, fg="#526b7e", font=("Arial", 10), justify="left", wraplength=430).pack(anchor="w", pady=(3, 0))

    def send_message(self):
        #Save the new plain-text message and refresh the conversation.
        text = self.message_text.get().strip()
        #Checks whether a recipient has been selected.
        if not self.recipient:
            self.send_status.set("Choose a user before sending a message.")
            return
        #Checks whether a message has been entered.
        if not text:
            self.send_status.set("Write a message before sending.")
            self.message_entry.focus_set()
            return
        #Converts the message to JSON format and sends it to the server.
        data = json.dumps(
            {"sender": self.username, "recipient": self.recipient, "content": text}
        ).encode("utf-8")
        #The request is built and sent to the messages endpoint on the server.
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
                body = json.loads(response.read().decode("utf-8"))
        #Standard catchall below
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                self.send_status.set(body.get("message", "Message could not be sent."))
            except json.JSONDecodeError:
                self.send_status.set("Message could not be sent.")
            return
        except (URLError, TimeoutError, json.JSONDecodeError):
            self.send_status.set("Could not reach the server. Try again.")
            return
        #This finally checks if the message was sent successfully.
        if not body.get("success"):
            self.send_status.set(body.get("message", "Message could not be sent."))
            return
        self.message_text.set("")
        self.send_status.set("")
        self._refresh_conversation(scroll_to_latest=True)

    def _send_from_enter(self, event):
        #Send the message when Enter is pressed in the message field.
        self.send_message()
        return "break"
        #Break stops double inputs
