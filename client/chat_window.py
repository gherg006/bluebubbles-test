import json
import tkinter as tk
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    def __init__(self, root, username, server_url):\
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
        self._build_window()
        self._load_users()
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
        #Build the large left conversation panel and message entry bar.
        chat = tk.Frame(parent, bg=BACKGROUND)
        chat.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        chat.grid_columnconfigure(0, weight=1)
        chat.grid_rowconfigure(1, weight=1)
        #Displays what username you are logged in as
        tk.Label(
            chat,
            text=f"Logged in as: {self.username}",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Arial", 9, "bold"),
        ).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        #

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

        #
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

    def _build_sorting(self, parent):
        #Keep the sorting actions in their own right-hand box.
        panel = tk.Frame(parent, bg=PANEL, bd=1, relief="solid", padx=8, pady=8)
        panel.grid(row=0, column=1, sticky="ns", padx=(24, 0))
        #Create the buttons for the sorting actions.
        for row, (label, command) in enumerate((("Most Recent", self._keep_order), ("Alphabetical", self._sort_alphabetical), ("Frequency", self._keep_order), ("Date Added", self._keep_order), ("New Messages", self._keep_order))):
            tk.Button(panel, text=label, command=command, bg=BUTTON, fg=TEXT, font=("Arial", 9), bd=1, relief="solid", width=16, pady=10).grid(
                row=row, column=0, pady=(0, 8), sticky="ew"
            )

    def _label(self, parent, text, row, column, **options):
        #Create a square blue label
        tk.Label(parent, text=text, bg=BUTTON, fg=TEXT, bd=1, relief="solid", **options).grid(row=row, column=column, sticky="ew", pady=(0, 8))

    def _load_users(self):
        #Ask the server for usernames (used by the add user menu).
        try:
            with urlopen(f"{self.server_url}/users", timeout=5) as response:
                self.users = json.loads(response.read().decode("utf-8")).get("users", [])
                #The client sends it to the endpoint to update the list of users.
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            self.users = []
        self._filter_users()
        #The return from the server is appended to the list of users.
        #It is then filtered through based on the selected filter.

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
        #Preserve database order until messages are stored by the server.
        self._filter_users()

    def _select_user(self, event):
        #Change the current chat label when a different user is selected.
        selected = self.user_list.curselection()
        if selected:
            self._open_conversation(self.user_list.get(selected[0]))

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
            self.contacts.append(user)
            self._filter_users()
            self._open_conversation(user)
            menu.destroy()

        tk.Button(menu, text="Add", command=add_selected_user, bg=BUTTON, fg=TEXT, bd=1, relief="solid", padx=18).pack(pady=(10, 0))

    def _open_conversation(self, recipient):
        #Load the selected account's saved conversation immediately.
        self.recipient = recipient
        self.current_messages = None
        self.send_status.set("")
        self.chat_title.set(f"Chat user: {recipient}")
        self._refresh_conversation(scroll_to_latest=True)
        #The conversation is loaded and the chat is displayed straight away.

    def _fetch_conversation(self):
        #Retrieve the active conversation without changing the visible chat.
        parameters = urlencode({"username": self.username, "with": self.recipient})
        #The subroutine tries to connect to the server and retrieve the conversation history.
        #It does this through the messages endpoint on the server.
        try:
            with urlopen(f"{self.server_url}/messages?{parameters}", timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("messages", [])
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return None
        #This returns the json messages, and if there are any errors it returns nothing.

    def _refresh_conversation(self, scroll_to_latest=False):
        #Redraw only when polling finds a new or changed message.
        saved_messages = self._fetch_conversation()
        if saved_messages is None or saved_messages == self.current_messages:
            return
        was_at_latest = self.message_canvas.yview()[1] >= 0.99
        self._clear_messages()
        #This fetches the conversation history from the server and redraws the messages.
        for message in saved_messages:
            self._add_message(
                message["sender"],
                message["content"],
                message["time"],
                message["sender"] != self.username,
            )
            #All the metadata of the message is fetched.
        if not saved_messages:
            self._add_message(self.recipient, "No messages yet.", "", True)
        self.current_messages = saved_messages
        if scroll_to_latest or was_at_latest:
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
        #Keep the scroll bar sized to the complete conversation.
        self.message_canvas.configure(scrollregion=self.message_canvas.bbox("all"))

    def _resize_message_frame(self, event):
        #Keep the message rows as wide as the conversation area.
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
        #This if statement ensures that it works on mac and windows
        if getattr(event, "delta", 0):
            amount = -int(event.delta / 120)
        else:
        #Since the server is running linux, I felt that it would make sense to do this for linux as well.
            amount = -1 if event.num == 4 else 1
        self.message_canvas.yview_scroll(amount, "units")
        return "break"
        #Finally returning the break string stops double scrolling or any other artefacts.

    def _scroll_to_latest(self):
        #Show the most recent message after loading or sending a conversation.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(1)

    def _show_empty_conversation(self):
        #Explain why the chat area is empty before an account is selected.
        self._add_message("BlueBubbles", "Choose an account from the users list to start chatting.", "", True)

    def _add_message(self, sender, text, time, incoming):
        #Add a plain message line to the conversation area.
        row = self.messages.grid_size()[1]
        #The message box is added where the message itself is added into the box.
        block = tk.Frame(self.messages, bg=WHITE)
        block.grid(row=row, column=0, sticky="w" if incoming else "e", padx=10, pady=(10, 0))
        tk.Label(block, text=f"{sender}    {time}".strip(), bg=WHITE, fg=TEXT, font=("Arial", 9, "bold")).pack(anchor="w")
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
        #The request is build and sent to the messages endpoint on the server.
        request = Request(
            f"{self.server_url}/messages",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        #Exception handling used to catch any errors.
        #This is crucial to ensure that the program can handle errors gracefully.
        try:
            with urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        #Below are the specific errors that can occur in this specific method.
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                self.send_status.set(body.get("message", "Message could not be sent."))
            except json.JSONDecodeError:
                self.send_status.set("Message could not be sent.")
            return
        #Below are the standard errors that are caught.
        except (URLError, TimeoutError, json.JSONDecodeError):
            self.send_status.set("Could not reach the server. Try again.")
            return

        #This finally checks if the message was sent successfully.
        if not body.get("success"):
            self.send_status.set(body.get("message", "Message could not be sent."))
            return
        self.message_text.set("")
        self.send_status.set("")
        self._open_conversation(self.recipient)

    def _send_from_enter(self, event):
        #Send the message when Enter is pressed in the message field.
        self.send_message()
        return "break"
        #Similar as before it stops double inputs.
