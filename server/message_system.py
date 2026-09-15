class MessageSystem:
    # Store and retrieve plain-text messages using the existing messages table.
    def __init__(self, run_query):
        self.run_query = run_query

    def send(self, sender, recipient, content):
        # Save a message only when both account names exist.
        result = self.run_query(
            "INSERT INTO messages (sender_id, reciepient_id, message_content, sent_at) "
            "SELECT sender.\"userID\", recipient.\"userID\", :'content', CURRENT_TIMESTAMP "
            "FROM users sender JOIN users recipient ON TRUE "
            "WHERE sender.username = :'sender' AND recipient.username = :'recipient' "
            "RETURNING message_id;",
            {"sender": sender, "recipient": recipient, "content": content},
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def conversation(self, username, other_user):
        # Return the messages shared by the two selected accounts.
        result = self.run_query(
            "SELECT sender.username, messages.message_content, "
            "to_char(messages.sent_at, 'HH24:MI') "
            "FROM messages "
            "JOIN users sender ON sender.\"userID\" = messages.sender_id "
            "JOIN users recipient ON recipient.\"userID\" = messages.reciepient_id "
            "WHERE (sender.username = :'username' AND recipient.username = :'other_user') "
            "OR (sender.username = :'other_user' AND recipient.username = :'username') "
            "ORDER BY messages.sent_at, messages.message_id;",
            {"username": username, "other_user": other_user},
        )
        if result.returncode != 0:
            return []

        messages = []
        for row in result.stdout.splitlines():
            sender, content, sent_at = row.split("|", 2)
            messages.append({"sender": sender, "content": content, "time": sent_at})
        return messages
